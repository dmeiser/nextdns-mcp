"""Unit tests for OpenAPI specification loading."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml


class TestLoadOpenApiSpec:
    """Test the load_openapi_spec() function."""

    def test_load_valid_openapi_spec(self, mock_openapi_spec, monkeypatch, mock_api_key):
        """Test loading a valid OpenAPI specification."""
        # Set required API key
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        # Create temp spec file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(mock_openapi_spec, f)
            temp_spec = Path(f.name)

        try:
            # Mock __file__ to point to temp location
            with patch("nextdns_mcp.server.Path") as mock_path:
                # Make Path(__file__).parent return the temp directory
                mock_path.return_value.parent = temp_spec.parent
                mock_parent = Mock()
                mock_parent.__truediv__ = Mock(return_value=temp_spec)
                mock_parent.exists = Mock(return_value=True)
                mock_path.return_value.parent = mock_parent

                from nextdns_mcp.server import load_openapi_spec

                result = load_openapi_spec()

                assert result == mock_openapi_spec
                assert result["openapi"] == "3.0.3"
                assert result["info"]["title"] == "Test NextDNS API"
        finally:
            temp_spec.unlink(missing_ok=True)

    def test_load_openapi_spec_file_not_found(self, monkeypatch, mock_api_key, capsys):
        """Test error handling when OpenAPI spec file is missing."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        with patch("nextdns_mcp.server.Path") as mock_path:
            # Mock the path to not exist
            mock_spec_path = Mock()
            mock_spec_path.exists.return_value = False
            mock_spec_path.__str__ = Mock(return_value="/fake/path/spec.yaml")

            mock_parent = Mock()
            mock_parent.__truediv__ = Mock(return_value=mock_spec_path)
            mock_path.return_value.parent = mock_parent

            from nextdns_mcp.server import load_openapi_spec

            with pytest.raises(SystemExit) as exc_info:
                load_openapi_spec()

            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "ERROR: OpenAPI spec not found" in captured.err

    def test_load_openapi_spec_invalid_yaml(self, monkeypatch, mock_api_key, capsys):
        """Test error handling for invalid YAML."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        # Create temp file with invalid YAML
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("invalid: yaml: content: {{{")
            temp_spec = Path(f.name)

        try:
            with patch("nextdns_mcp.server.Path") as mock_path:
                mock_spec_path = temp_spec
                mock_parent = Mock()
                mock_parent.__truediv__ = Mock(return_value=mock_spec_path)
                mock_path.return_value.parent = mock_parent

                from nextdns_mcp.server import load_openapi_spec

                # Should raise yaml.YAMLError
                with pytest.raises(yaml.YAMLError):
                    load_openapi_spec()
        finally:
            temp_spec.unlink(missing_ok=True)

    def test_load_openapi_spec_prints_path(
        self, mock_openapi_spec, monkeypatch, mock_api_key, capsys
    ):
        """Test that loading prints the spec path to stderr."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(mock_openapi_spec, f)
            temp_spec = Path(f.name)

        try:
            with patch("nextdns_mcp.server.Path") as mock_path:
                mock_spec_path = temp_spec
                mock_parent = Mock()
                mock_parent.__truediv__ = Mock(return_value=mock_spec_path)
                mock_path.return_value.parent = mock_parent

                from nextdns_mcp.server import load_openapi_spec

                load_openapi_spec()

                captured = capsys.readouterr()
                assert "Loading OpenAPI spec from:" in captured.err
        finally:
            temp_spec.unlink(missing_ok=True)

    def test_load_openapi_spec_returns_dict(self, mock_openapi_spec, monkeypatch, mock_api_key):
        """Test that load_openapi_spec returns a dictionary."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(mock_openapi_spec, f)
            temp_spec = Path(f.name)

        try:
            with patch("nextdns_mcp.server.Path") as mock_path:
                mock_spec_path = temp_spec
                mock_parent = Mock()
                mock_parent.__truediv__ = Mock(return_value=mock_spec_path)
                mock_path.return_value.parent = mock_parent

                from nextdns_mcp.server import load_openapi_spec

                result = load_openapi_spec()

                assert isinstance(result, dict)
                assert "openapi" in result
                assert "paths" in result
        finally:
            temp_spec.unlink(missing_ok=True)
