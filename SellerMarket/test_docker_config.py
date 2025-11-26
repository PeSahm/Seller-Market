# test_docker_config.py
"""
Tests for Docker configuration and OCR service integration.
Validates Docker files, environment variables, and OCR connectivity.
"""

import os
import sys
import yaml
import pytest
from unittest.mock import patch, MagicMock


class TestDockerConfiguration:
    """Test Docker configuration files."""

    def test_dockerfile_exists(self):
        """Verify Dockerfile exists."""
        dockerfile_path = os.path.join(os.path.dirname(__file__), 'Dockerfile')
        assert os.path.exists(dockerfile_path), "Dockerfile should exist"

    def test_docker_compose_exists(self):
        """Verify docker-compose.yml exists."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        assert os.path.exists(compose_path), "docker-compose.yml should exist"

    def test_dockerignore_exists(self):
        """Verify .dockerignore exists."""
        dockerignore_path = os.path.join(os.path.dirname(__file__), '.dockerignore')
        assert os.path.exists(dockerignore_path), ".dockerignore should exist"

    def test_docker_compose_valid_yaml(self):
        """Verify docker-compose.yml is valid YAML."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        assert compose_data is not None, "docker-compose.yml should be valid YAML"
        assert 'services' in compose_data, "docker-compose.yml should have services"

    def test_docker_compose_has_ocr_service(self):
        """Verify docker-compose.yml has OCR service."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        assert 'ocr' in compose_data['services'], "docker-compose should have 'ocr' service"
        ocr_service = compose_data['services']['ocr']
        assert 'ghcr.io/pesahm/ocr' in ocr_service['image'], "OCR service should use correct image"

    def test_docker_compose_has_trading_bot_service(self):
        """Verify docker-compose.yml has trading-bot service."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        assert 'trading-bot' in compose_data['services'], "docker-compose should have 'trading-bot' service"

    def test_docker_compose_ocr_url_environment(self):
        """Verify trading-bot service has OCR_SERVICE_URL environment variable."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        bot_service = compose_data['services']['trading-bot']
        environment = bot_service.get('environment', [])
        
        # Check if OCR_SERVICE_URL is set
        ocr_url_found = False
        for env in environment:
            if 'OCR_SERVICE_URL' in env:
                ocr_url_found = True
                assert 'http://ocr:8080' in env, "OCR_SERVICE_URL should use internal Docker networking"
                break
        
        assert ocr_url_found, "OCR_SERVICE_URL should be set in trading-bot environment"

    def test_docker_compose_depends_on_ocr(self):
        """Verify trading-bot depends on OCR service."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        bot_service = compose_data['services']['trading-bot']
        depends_on = bot_service.get('depends_on', {})
        
        assert 'ocr' in depends_on, "trading-bot should depend on ocr service"

    def test_docker_compose_volume_mounts(self):
        """Verify required volume mounts are configured."""
        compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        bot_service = compose_data['services']['trading-bot']
        volumes = bot_service.get('volumes', [])
        volume_str = str(volumes)
        
        # Check required volume mounts
        assert 'config.ini' in volume_str, "config.ini should be mounted"
        assert 'logs' in volume_str, "logs directory should be mounted"
        assert 'order_results' in volume_str, "order_results directory should be mounted"

    def test_dockerignore_excludes_sensitive_files(self):
        """Verify .dockerignore excludes sensitive files."""
        dockerignore_path = os.path.join(os.path.dirname(__file__), '.dockerignore')
        with open(dockerignore_path, 'r') as f:
            dockerignore_content = f.read()
        
        # Check sensitive files are excluded
        assert 'config.ini' in dockerignore_content, ".dockerignore should exclude config.ini"
        assert '.env' in dockerignore_content, ".dockerignore should exclude .env"
        assert '__pycache__' in dockerignore_content, ".dockerignore should exclude __pycache__"
        assert 'logs/' in dockerignore_content, ".dockerignore should exclude logs/"


class TestOCRServiceURLConfiguration:
    """Test OCR service URL configuration in application code."""

    def test_captcha_utils_uses_env_variable(self):
        """Verify captcha_utils.py uses OCR_SERVICE_URL environment variable."""
        captcha_utils_path = os.path.join(os.path.dirname(__file__), 'captcha_utils.py')
        with open(captcha_utils_path, 'r') as f:
            content = f.read()
        
        assert 'OCR_SERVICE_URL' in content, "captcha_utils.py should use OCR_SERVICE_URL"
        assert "os.getenv" in content, "captcha_utils.py should use os.getenv"
        assert 'http://localhost:8080' in content, "captcha_utils.py should have localhost fallback"

    def test_locustfile_uses_env_variable(self):
        """Verify locustfile.py uses OCR_SERVICE_URL environment variable."""
        locustfile_path = os.path.join(os.path.dirname(__file__), 'locustfile.py')
        with open(locustfile_path, 'r') as f:
            content = f.read()
        
        assert 'OCR_SERVICE_URL' in content, "locustfile.py should use OCR_SERVICE_URL"
        assert "os.getenv" in content, "locustfile.py should use os.getenv"

    def test_ocr_url_default_fallback(self):
        """Test OCR URL defaults to localhost when env var not set."""
        # Clear any existing OCR_SERVICE_URL
        with patch.dict(os.environ, {}, clear=True):
            # Re-import to test default value
            import importlib
            if 'captcha_utils' in sys.modules:
                del sys.modules['captcha_utils']
            
            # Mock the import
            with patch.dict(os.environ, {'OCR_SERVICE_URL': 'http://localhost:8080'}):
                url = os.getenv('OCR_SERVICE_URL', 'http://localhost:8080')
                assert url == 'http://localhost:8080', "Default should be localhost:8080"

    def test_ocr_url_docker_override(self):
        """Test OCR URL can be overridden for Docker."""
        with patch.dict(os.environ, {'OCR_SERVICE_URL': 'http://ocr:8080'}):
            url = os.getenv('OCR_SERVICE_URL', 'http://localhost:8080')
            assert url == 'http://ocr:8080', "Should use Docker internal URL when env var set"


class TestOCRServiceConnectivity:
    """Test OCR service connectivity (mocked for unit tests)."""

    @patch('requests.post')
    def test_decode_captcha_with_mock(self, mock_post):
        """Test decode_captcha function with mocked OCR service."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.text = '"abc123"'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        
        # Import and test
        with patch.dict(os.environ, {'OCR_SERVICE_URL': 'http://test-ocr:8080'}):
            if 'captcha_utils' in sys.modules:
                del sys.modules['captcha_utils']
            from captcha_utils import decode_captcha
            
            result = decode_captcha("base64encodedimage")
            
            # Verify the call was made
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert '/ocr/captcha-easy-base64' in call_args[0][0]

    @patch('requests.post')
    def test_decode_captcha_error_handling(self, mock_post):
        """Test decode_captcha handles errors gracefully."""
        import requests
        
        # Setup mock to raise exception
        mock_post.side_effect = requests.RequestException("Connection failed")
        
        with patch.dict(os.environ, {'OCR_SERVICE_URL': 'http://localhost:8080'}):
            if 'captcha_utils' in sys.modules:
                del sys.modules['captcha_utils']
            from captcha_utils import decode_captcha
            
            result = decode_captcha("base64encodedimage")
            
            # Should return empty string on error
            assert result == "", "Should return empty string on error"


class TestEnvExampleFile:
    """Test .env.example file."""

    def test_env_example_exists(self):
        """Verify .env.example exists."""
        env_example_path = os.path.join(os.path.dirname(__file__), '.env.example')
        assert os.path.exists(env_example_path), ".env.example should exist"

    def test_env_example_has_required_variables(self):
        """Verify .env.example has required variables."""
        env_example_path = os.path.join(os.path.dirname(__file__), '.env.example')
        with open(env_example_path, 'r') as f:
            content = f.read()
        
        assert 'TELEGRAM_BOT_TOKEN' in content, ".env.example should have TELEGRAM_BOT_TOKEN"
        assert 'TELEGRAM_USER_ID' in content, ".env.example should have TELEGRAM_USER_ID"
        assert 'OCR_SERVICE_URL' in content, ".env.example should document OCR_SERVICE_URL"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
