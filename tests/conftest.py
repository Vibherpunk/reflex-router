import pytest
from server import app
from auth import verify_gateway_token

@pytest.fixture(autouse=True)
def mock_auth():
    app.dependency_overrides[verify_gateway_token] = lambda: None
    yield
    app.dependency_overrides.clear()
