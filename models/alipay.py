# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2010-2015 Elico Corp (<http://www.elico-corp.com>)
#    Chen Rong <chen.rong@elico-corp.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
import base64
try:
    import simplejson as json
except ImportError:
    import json
import logging
import urlparse
import werkzeug.urls
import urllib2
from hashlib import md5
from openerp.addons.payment.models.payment_acquirer import ValidationError
from openerp.addons.payment_alipay.controllers.main import AlipayController
from openerp.osv import osv, fields
from openerp.tools.float_utils import float_compare
from openerp import SUPERUSER_ID

_logger = logging.getLogger(__name__)

class AcquirerAlipay(osv.Model):
    _inherit = 'payment.acquirer'

    def _get_alipay_urls(self, cr, uid, environment, context=None):
        """ Alipay URLs
        """
        return {
            'alipay_form_url': 'https://mapi.alipay.com/gateway.do',
        }

    def _get_providers(self, cr, uid, context=None):
        providers = super(AcquirerAlipay, self)._get_providers(cr, uid, context=context)
        providers.append(['alipay', 'Alipay'])
        return providers

    _columns = {
        'alipay_pid': fields.char('PID', required_if_provider='alipay'),
        'alipay_key': fields.char('Key', required_if_provider='alipay'),
        'alipay_seller_email': fields.char('Seller Email', required_if_provider='alipay'),
        'logistics_type': fields.char('Logistics Type', required_if_provider='alipay'), 
        'logistics_fee': fields.char('Logistics Fee', required_if_provider='alipay'), 
        'logistics_payment': fields.char('Logistics Payment', required_if_provider='alipay'), 
        'service':fields.selection([
         ('create_direct_pay_by_user', "create_direct_pay_by_user"),
         ('create_partner_trade_by_buyer', "create_partner_trade_by_buyer"),
    ],'Payment Type', required_if_provider='alipay'),
    }

    def _alipay_generate_md5_sign(self, acquirer, inout, values):
        """ Generate the md5sign for incoming or outgoing communications.

        :param browse acquirer: the payment.acquirer browse record. It should
                                have a md5key in shaky out
        :param string inout: 'in' (openerp contacting alipay) or 'out' (alipay
                             contacting openerp).
        :param dict values: transaction values

        :return string: md5sign
        """
        assert inout in ('in', 'out')
        assert acquirer.provider == 'alipay'

        alipay_key = acquirer.alipay_key

        if inout == 'out':
            keys = ['buyer_email', 'buyer_id', 'exterface', 'is_success', 'notify_id', 'notify_time', 'notify_type','out_trade_no','payment_type', 'seller_email','seller_id', 'subject', 'total_fee', 'trade_no', 'trade_status']
            src = '&'.join(['%s=%s' % (key, value) for key,
                        value in sorted(values.items()) if key in keys]) + alipay_key
        else:
            if values['service'] == 'create_direct_pay_by_user':
                keys = ['return_url', 'notify_url', '_input_charset','partner','payment_type','seller_email','service','out_trade_no','subject','total_fee']
                src = '&'.join(['%s=%s' % (key, value) for key,
                            value in sorted(values.items()) if key in keys])  + alipay_key
            elif values['service'] == 'create_partner_trade_by_buyer':
                keys = ['return_url', 'notify_url', 'logistics_type','logistics_fee', 'logistics_payment', 'price', 'quantity', '_input_charset','partner','payment_type','seller_email','service','out_trade_no','subject']
                src = '&'.join(['%s=%s' % (key, value) for key,value in sorted(values.items()) if key in keys])  + alipay_key
        return md5(src.encode('utf-8')).hexdigest()

    def alipay_form_generate_values(self, cr, uid, id, partner_values, tx_values, context=None):
        base_url = self.pool['ir.config_parameter'].get_param(cr, SUPERUSER_ID, 'web.base.url')
        acquirer = self.browse(cr, uid, id, context=context)

        alipay_tx_values = dict(tx_values)
        alipay_tx_values.update({
            'seller_email': acquirer.alipay_seller_email,
            '_input_charset': 'utf-8',
            'partner': acquirer.alipay_pid,
            'payment_type': '1',
            'service': acquirer.service,
            'sign_type': 'MD5',
            'out_trade_no': tx_values['reference'],
            'total_fee': tx_values['amount'],
            'subject': tx_values['reference'],
            'price': tx_values['amount'],
            'quantity': '1',
            'logistics_fee':acquirer.logistics_fee,
            'logistics_payment':acquirer.logistics_payment,
            'logistics_type':acquirer.logistics_type,
            'return_url': '%s' % urlparse.urljoin(base_url, AlipayController._return_url),
            'notify_url': '%s' % urlparse.urljoin(base_url, AlipayController._notify_url),
            'cancel_return': '%s' % urlparse.urljoin(base_url, AlipayController._cancel_url),
        })
        alipay_tx_values['sign'] = self._alipay_generate_md5_sign(acquirer, 'in', alipay_tx_values)
        return partner_values, alipay_tx_values

    def alipay_get_form_action_url(self, cr, uid, id, context=None):
        acquirer = self.browse(cr, uid, id, context=context)
        return self._get_alipay_urls(cr, uid, acquirer.environment, context=context)['alipay_form_url']

class TxAlipay(osv.Model):
    _inherit = 'payment.transaction'

    _columns = {
        'alipay_txn_tradeno': fields.char('Transaction Trade Number'),
    }

    # --------------------------------------------------
    # FORM RELATED METHODS
    # --------------------------------------------------

    def _alipay_form_get_tx_from_data(self, cr, uid, data, context=None):
        """ Given a data dict coming from alipay, verify it and find the related
        transaction record. """
        reference = data.get('out_trade_no')
        if not reference:
            error_msg = 'Alipay: received data with missing reference (%s)' % reference
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        
        tx_ids = self.pool['payment.transaction'].search(cr, uid, [('reference', '=', reference)], context=context)
        if not tx_ids or len(tx_ids) > 1:
            error_msg = 'Alipay: received data for reference %s' % (reference)
            if not tx_ids:
                error_msg += '; no order found'
            else:
                error_msg += '; multiple order found'
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        tx = self.pool['payment.transaction'].browse(cr, uid, tx_ids[0], context=context)

        sign_check = self.pool['payment.acquirer']._alipay_generate_md5_sign(tx.acquirer_id, 'out', data)
        if sign_check != data.get('sign'):
            error_msg = 'alipay: invalid md5str, received %s, computed %s' % (data.get('sign'), sign_check)
            _logger.warning(error_msg)
            raise ValidationError(error_msg)

        return tx

    def _alipay_form_get_invalid_parameters(self, cr, uid, tx, data, context=None):
        invalid_parameters = []

        if tx.acquirer_reference and data.get('out_trade_no') != tx.acquirer_reference:
            invalid_parameters.append(('Transaction Id', data.get('out_trade_no'), tx.acquirer_reference))

        if float_compare(float(data.get('total_fee', '0.0')), tx.amount, 2) != 0:
            invalid_parameters.append(('Amount', data.get('total_fee'), '%.2f' % tx.amount))

        return invalid_parameters

    def _alipay_form_validate(self, cr, uid, tx, data, context=None):
        if data.get('trade_status') in ['TRADE_SUCCESS','TRADE_FINISHED']:
            tx.write({
                'state': 'done',
                'acquirer_reference': data.get('out_trade_no'),
                'alipay_txn_tradeno': data.get('trade_no'),
                'date_validate': data.get('notify_time'),
            })
            return True
        else:
            error = 'Alipay: feedback error.'
            _logger.info(error)
            tx.write({
                'state': 'error',
                'state_message': error,
            })
            return False
