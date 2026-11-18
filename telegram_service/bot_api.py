"""Minimal Telegram Bot API adapter; preserve safe diagnostics without leaking tokens."""
import json
import re
import urllib.error
import urllib.request

_BOT_TOKEN = re.compile(r"\b[0-9]{5,12}:[A-Za-z0-9_-]{24,}\b")


def safe_description(value, token: str = "") -> str | None:
    """Telegram error text only, trimmed and scrubbed before external exposure."""
    if not isinstance(value, str) or not value.strip():
        return None
    clean = value.replace(token, "[redacted]") if token else value
    clean = _BOT_TOKEN.sub("[redacted]", clean)
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", clean).strip()
    return clean[:300] if clean else None


class BotApiError(Exception):
    def __init__(self, code: str, uncertain: bool = False,
                 error_code: int | None = None, description: str | None = None):
        self.code = code  # stable internal category, e.g. telegram_http_400
        self.uncertain = uncertain
        self.error_code = error_code  # Telegram Bot API numeric error_code
        self.description = description
        super().__init__(code)


class BotApi:
    def __init__(self, base_url: str = "https://api.telegram.org"):
        self.base_url = base_url.rstrip("/")

    def call(self, token: str, method: str, payload: dict | None = None) -> dict | list | bool:
        url = f"{self.base_url}/bot{token}/{method}"
        request = urllib.request.Request(
            url, data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            # Telegram HTTP errors include a JSON {"error_code", "description"}.
            # Never stringify urllib exceptions: their URL embeds the bot token.
            code = exc.code
            description = None
            try:
                detail = json.loads(exc.read(4096))
                if isinstance(detail, dict):
                    if type(detail.get("error_code")) is int and 400 <= detail["error_code"] <= 599:
                        code = detail["error_code"]
                    description = safe_description(detail.get("description"), token)
            except (ValueError, TypeError, OSError):
                pass
            raise BotApiError(f"telegram_http_{code}", error_code=code,
                              description=description) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Request may have reached Telegram before response was lost.
            raise BotApiError("telegram_outcome_unknown", uncertain=True) from None
        except (ValueError, KeyError, TypeError):
            raise BotApiError("telegram_invalid_response", uncertain=True) from None
        if not isinstance(value, dict):
            raise BotApiError("telegram_invalid_response", uncertain=True)
        if not value.get("ok"):
            number = value.get("error_code")
            number = number if type(number) is int else None
            raise BotApiError(
                f"telegram_http_{number}" if number is not None else "telegram_rejected",
                error_code=number, description=safe_description(value.get("description"), token))
        result = value.get("result")
        if not isinstance(result, (dict, list, bool)):
            raise BotApiError("telegram_invalid_response", uncertain=True)
        return result
