"""GitHub and Google sign-in via Authlib. Only providers with credentials configured are enabled."""

from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth

from .config import Settings


@dataclass
class Profile:
    sub: str
    email: str | None
    email_verified: bool


def build(settings: Settings) -> OAuth:
    oauth = OAuth()
    if settings.github_client_id and settings.github_client_secret:
        oauth.register(
            "github", client_id=settings.github_client_id, client_secret=settings.github_client_secret,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/", client_kwargs={"scope": "read:user user:email"},
        )
    if settings.google_client_id and settings.google_client_secret:
        oauth.register(
            "google", client_id=settings.google_client_id, client_secret=settings.google_client_secret,
            access_token_url="https://oauth2.googleapis.com/token",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            api_base_url="https://openidconnect.googleapis.com/v1/",
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth


def enabled(settings: Settings) -> list[str]:
    return [p for p in ("github", "google")
            if getattr(settings, f"{p}_client_id") and getattr(settings, f"{p}_client_secret")]


async def fetch_profile(oauth: OAuth, provider: str, request) -> Profile:
    client = getattr(oauth, provider)
    token = await client.authorize_access_token(request)
    if provider == "github":
        user = (await client.get("user", token=token)).json()
        emails = (await client.get("user/emails", token=token)).json()
        primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
        return Profile(str(user["id"]), primary["email"].lower() if primary else None, bool(primary))
    info = (await client.get("userinfo", token=token)).json()
    return Profile(str(info["sub"]), (info.get("email") or "").lower() or None, bool(info.get("email_verified")))
