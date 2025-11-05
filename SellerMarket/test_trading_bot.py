"""
Unit tests for the trading bot components.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import json
from datetime import datetime, timedelta

from broker_enum import BrokerCode
from api_client import EphoenixAPIClient
from order_tracker import OrderResult, OrderResultTracker


class TestBrokerEnum(unittest.TestCase):
    """Test BrokerCode enumeration."""
    
    def test_broker_codes(self):
        """Test broker code values."""
        self.assertEqual(BrokerCode.GANJINE.value, "gs")
        self.assertEqual(BrokerCode.SHAHR.value, "shahr")
        self.assertEqual(BrokerCode.BOURSE_BIME.value, "bbi")
    
    def test_is_valid(self):
        """Test broker code validation."""
        self.assertTrue(BrokerCode.is_valid("gs"))
        self.assertTrue(BrokerCode.is_valid("bbi"))
        self.assertFalse(BrokerCode.is_valid("invalid"))
    
    def test_get_broker_name(self):
        """Test broker name retrieval."""
        name = BrokerCode.get_broker_name("gs")
        self.assertIn("Ghadir", name)
    
    def test_get_endpoints(self):
        """Test endpoint generation."""
        endpoints = BrokerCode.GANJINE.get_endpoints()
        
        self.assertIn('captcha', endpoints)
        self.assertIn('login', endpoints)
        self.assertIn('order', endpoints)
        self.assertIn('gs.ephoenix.ir', endpoints['login'])


class TestAPIClient(unittest.TestCase):
    """Test EphoenixAPIClient."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.broker_code = "gs"
        self.username = "test_user"
        self.password = "test_pass"
        self.captcha_decoder = Mock(return_value="12345")
        self.endpoints = BrokerCode.GANJINE.get_endpoints()
        
        self.client = EphoenixAPIClient(
            broker_code=self.broker_code,
            username=self.username,
            password=self.password,
            captcha_decoder=self.captcha_decoder,
            endpoints=self.endpoints
        )
    
    def test_initialization(self):
        """Test client initialization."""
        self.assertEqual(self.client.username, self.username)
        self.assertEqual(self.client.broker_code, self.broker_code)
        self.assertIsNone(self.client.token)
    
    def test_token_filename(self):
        """Test token filename generation."""
        filename = self.client._get_token_filename()
        self.assertIn(self.username, filename)
        self.assertIn("identity", filename)
    
    @patch('api_client.requests.get')
    def test_fetch_captcha(self, mock_get):
        """Test captcha fetching."""
        mock_response = Mock()
        mock_response.json.return_value = {
            'captchaByteData': 'base64data',
            'salt': 'salt123',
            'hashedCaptcha': 'hash123'
        }
        mock_get.return_value = mock_response
        
        result = self.client._fetch_captcha()
        
        self.assertEqual(result['salt'], 'salt123')
        self.assertEqual(result['hashed_captcha'], 'hash123')
        mock_get.assert_called_once()
    
    @patch('api_client.requests.post')
    @patch('api_client.requests.get')
    def test_login_success(self, mock_get, mock_post):
        """Test successful login."""
        # Mock captcha response
        mock_captcha_response = Mock()
        mock_captcha_response.json.return_value = {
            'captchaByteData': 'base64data',
            'salt': 'salt123',
            'hashedCaptcha': 'hash123'
        }
        mock_get.return_value = mock_captcha_response
        
        # Mock login response
        mock_login_response = Mock()
        mock_login_response.json.return_value = {'token': 'test_token_123'}
        mock_post.return_value = mock_login_response
        
        token = self.client._login_with_captcha()
        
        self.assertEqual(token, 'test_token_123')
        self.captcha_decoder.assert_called_once()
    
    @patch('api_client.requests.get')
    def test_get_buying_power(self, mock_get):
        """Test buying power retrieval."""
        self.client.token = 'test_token'
        self.client.token_expiry = datetime.now() + timedelta(hours=1)
        
        mock_response = Mock()
        mock_response.json.return_value = {'buyingPower': 1000000}
        mock_get.return_value = mock_response
        
        buying_power = self.client.get_buying_power()
        
        self.assertEqual(buying_power, 1000000)
        mock_get.assert_called_once()
    
    @patch('api_client.requests.post')
    def test_get_instrument_info(self, mock_post):
        """Test instrument information retrieval."""
        self.client.token = 'test_token'
        self.client.token_expiry = datetime.now() + timedelta(hours=1)
        
        mock_response = Mock()
        mock_response.json.return_value = [{
            'i': {
                's': 'TEST',
                't': 'Test Stock',
                'maxeq': 100000,
                'mineq': 1
            },
            't': {
                'maxap': 1500,
                'minap': 1300,
                'cup': 1400
            }
        }]
        mock_post.return_value = mock_response
        
        info = self.client.get_instrument_info('IRO1TEST0001')
        
        self.assertEqual(info['symbol'], 'TEST')
        self.assertEqual(info['max_price'], 1500)
        self.assertEqual(info['min_price'], 1300)
        self.assertEqual(info['max_volume'], 100000)
    
    @patch('api_client.requests.post')
    def test_calculate_order_volume(self, mock_post):
        """Test order volume calculation."""
        self.client.token = 'test_token'
        self.client.token_expiry = datetime.now() + timedelta(hours=1)
        
        mock_response = Mock()
        mock_response.json.return_value = {
            'volume': 50000,
            'totalNetAmount': 1000000,
            'totalFee': 5000
        }
        mock_post.return_value = mock_response
        
        volume = self.client.calculate_order_volume(
            isin='IRO1TEST0001',
            side=1,
            buying_power=1000000,
            price=1500
        )
        
        self.assertEqual(volume, 50000)


class TestOrderResult(unittest.TestCase):
    """Test OrderResult class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.order_data = {
            'isin': 'IRO1TEST0001',
            'symbol': 'TEST',
            'symbolTitle': 'Test Stock',
            'trackingNumber': 123456,
            'serialNumber': 789,
            'created': '2025-11-05T10:00:00Z',
            'createdShamsiDate': '1404/08/15',
            'orderSide': 1,
            'price': 1500,
            'volume': 1000,
            'remainedVolume': 500,
            'executedVolume': 500,
            'state': 2,
            'stateDesc': 'Partially Filled',
            'isDone': False,
            'netAmount': 1500000
        }
    
    def test_initialization(self):
        """Test order result initialization."""
        order = OrderResult(self.order_data)
        
        self.assertEqual(order.isin, 'IRO1TEST0001')
        self.assertEqual(order.symbol, 'TEST')
        self.assertEqual(order.tracking_number, 123456)
        self.assertEqual(order.side, 1)
        self.assertEqual(order.side_desc, 'Buy')
    
    def test_to_dict(self):
        """Test conversion to dictionary."""
        order = OrderResult(self.order_data)
        result = order.to_dict()
        
        self.assertIsInstance(result, dict)
        self.assertEqual(result['isin'], 'IRO1TEST0001')
        self.assertEqual(result['tracking_number'], 123456)
    
    def test_string_representation(self):
        """Test string representation."""
        order = OrderResult(self.order_data)
        string = str(order)
        
        self.assertIn('123456', string)
        self.assertIn('Buy', string)
        self.assertIn('TEST', string)


class TestOrderResultTracker(unittest.TestCase):
    """Test OrderResultTracker class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.tracker = OrderResultTracker(results_dir="test_results")
        self.order_data = {
            'isin': 'IRO1TEST0001',
            'symbol': 'TEST',
            'symbolTitle': 'Test Stock',
            'trackingNumber': 123456,
            'serialNumber': 789,
            'created': '2025-11-05T10:00:00Z',
            'createdShamsiDate': '1404/08/15',
            'orderSide': 1,
            'price': 1500,
            'volume': 1000,
            'remainedVolume': 500,
            'executedVolume': 500,
            'state': 2,
            'stateDesc': 'Partially Filled',
            'isDone': False,
            'netAmount': 1500000
        }
    
    def test_initialization(self):
        """Test tracker initialization."""
        self.assertTrue(self.tracker.results_dir.exists())
    
    @patch('order_tracker.Path.mkdir')
    @patch('builtins.open', create=True)
    def test_save_order_results(self, mock_open, mock_mkdir):
        """Test saving order results."""
        order = OrderResult(self.order_data)
        orders = [order]
        
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        
        self.tracker.save_order_results('test_user', 'gs', orders)
        
        mock_open.assert_called_once()
    
    def test_get_summary_report(self):
        """Test summary report generation."""
        # This will return no orders found
        report = self.tracker.get_summary_report('test_user', 'gs')
        
        self.assertIn('test_user', report)
        self.assertIn('gs', report)


class TestEndToEndFlow(unittest.TestCase):
    """Test end-to-end order flow simulation."""
    
    @patch('api_client.requests.post')
    @patch('api_client.requests.get')
    def test_complete_order_flow(self, mock_get, mock_post):
        """Test complete order placement flow."""
        # Setup mocks for authentication
        mock_captcha_response = Mock()
        mock_captcha_response.json.return_value = {
            'captchaByteData': 'base64data',
            'salt': 'salt123',
            'hashedCaptcha': 'hash123'
        }
        
        mock_login_response = Mock()
        mock_login_response.json.return_value = {'token': 'test_token'}
        
        mock_buying_power_response = Mock()
        mock_buying_power_response.json.return_value = {'buyingPower': 10000000}
        
        mock_instrument_response = Mock()
        mock_instrument_response.json.return_value = [{
            'i': {'s': 'TEST', 't': 'Test Stock', 'maxeq': 100000, 'mineq': 1},
            't': {'maxap': 1500, 'minap': 1300, 'cup': 1400}
        }]
        
        mock_volume_response = Mock()
        mock_volume_response.json.return_value = {
            'volume': 6500,
            'totalNetAmount': 10000000,
            'totalFee': 35000
        }
        
        # Configure mock responses
        mock_get.side_effect = [mock_captcha_response, mock_buying_power_response]
        mock_post.side_effect = [
            mock_login_response,
            mock_instrument_response,
            mock_volume_response
        ]
        
        # Create client
        captcha_decoder = Mock(return_value="12345")
        endpoints = BrokerCode.GANJINE.get_endpoints()
        client = EphoenixAPIClient(
            broker_code="gs",
            username="test_user",
            password="test_pass",
            captcha_decoder=captcha_decoder,
            endpoints=endpoints
        )
        
        # Execute flow
        token = client.authenticate()
        self.assertEqual(token, 'test_token')
        
        buying_power = client.get_buying_power()
        self.assertEqual(buying_power, 10000000)
        
        instrument_info = client.get_instrument_info('IRO1TEST0001')
        self.assertEqual(instrument_info['max_price'], 1500)
        
        volume = client.calculate_order_volume(
            isin='IRO1TEST0001',
            side=1,
            buying_power=buying_power,
            price=instrument_info['max_price']
        )
        self.assertEqual(volume, 6500)


if __name__ == '__main__':
    unittest.main()
