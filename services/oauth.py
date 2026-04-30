import httpx
import os

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET")
GITHUB_CLI_CLIENT_ID = os.environ.get("GITHUB_CLI_CLIENT_ID")
GITHUB_CLI_CLIENT_SECRET = os.environ.get("GITHUB_CLI_CLIENT_SECRET")



async def exchange_code_for_token(
    code: str,
    redirect_uri: str,
    code_verifier: str = None,
    use_cli_credentials: bool = False
) -> str:
    client_id = GITHUB_CLI_CLIENT_ID if use_cli_credentials else GITHUB_CLIENT_ID
    client_secret = GITHUB_CLI_CLIENT_SECRET if use_cli_credentials else GITHUB_CLIENT_SECRET

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            data=payload,
            headers={"Accept": "application/json"},
        )

    data = response.json()
    if "error" in data:
        raise ValueError(f"GitHub error: {data.get('error_description', data['error'])}")

    return data["access_token"]

async def get_github_user(github_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
            },
        )

    user_data = response.json()
    return {
        "github_id": str(user_data["id"]),
        "username": user_data["login"],
        "email": user_data.get("email"),
        "avatar_url": user_data.get("avatar_url"),
    }