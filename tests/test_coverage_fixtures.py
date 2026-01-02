"""Extra tests to exercise conftest fixtures and improve coverage."""

from pathlib import Path


def test_conftest_temp_files_exist(temp_api_key_file, temp_openapi_file, mock_openapi_spec):
    # Ensure temporary files created by fixtures exist and contain expected content
    assert isinstance(temp_api_key_file, Path)
    assert temp_api_key_file.exists()

    assert isinstance(temp_openapi_file, Path)
    assert temp_openapi_file.exists()

    # Basic check of the provided mock spec dict
    assert "openapi" in mock_openapi_spec


def test_conftest_env_and_profiles(
    clean_env, set_env_api_key, mock_profile_id, mock_profiles_response, mock_doh_response, mock_nextdns_base_url
):
    # Ensure fixtures return values as expected
    assert set_env_api_key is not None
    assert isinstance(mock_profile_id, str)
    assert "data" in mock_profiles_response
    assert mock_doh_response.get("Status") == 0
    assert mock_nextdns_base_url.startswith("https://")
