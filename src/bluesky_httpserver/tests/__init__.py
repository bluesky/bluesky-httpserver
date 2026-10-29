import httpx2

# RESPX currently imports and patches the ``httpx`` module. Register HTTPX2's
# migration alias before pytest loads conftest.py and its RESPX import.
httpx2.alias_httpx()
