from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from json import JSONDecodeError, loads
from urllib.parse import parse_qs, urlparse

from kite_core import Account, DATA_DIR


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoLoginCredentials:
    user_id: str
    password: str
    totp_secret: str


def is_auto_login_configured(account: Account) -> bool:
    try:
        credentials_for(account)
    except ValueError:
        return False
    return True


def credentials_for(account: Account) -> AutoLoginCredentials:
    suffix = env_suffix(account.label)
    user_id = account.login_user_id.strip() or account.user_id.strip() or first_env(
        f"KITE_USER_ID_{suffix}", "KITE_USER_ID"
    )
    password = account.password.strip() or first_env(f"KITE_PASSWORD_{suffix}", "KITE_PASSWORD")
    totp_secret = account.totp_secret.strip() or first_env(
        f"KITE_TOTP_SECRET_{suffix}", "KITE_TOTP_SECRET"
    )

    missing = []
    if not user_id:
        missing.append(f"KITE_USER_ID_{suffix} or KITE_USER_ID")
    if not password:
        missing.append(f"KITE_PASSWORD_{suffix} or KITE_PASSWORD")
    if not totp_secret:
        missing.append(f"KITE_TOTP_SECRET_{suffix} or KITE_TOTP_SECRET")

    if missing:
        raise ValueError(
            "Auto-login credentials are not configured. Missing: "
            + ", ".join(missing)
        )

    return AutoLoginCredentials(user_id=user_id, password=password, totp_secret=totp_secret)


def first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def env_suffix(label: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", label.strip()).strip("_").upper()
    return suffix or "DEFAULT"


def generate_totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:
        raise RuntimeError("pyotp is not installed. Run: pip install -r requirements.txt") from exc
    return pyotp.TOTP(secret).now()


def auto_login(account: Account, login_url: str | None = None) -> str:
    credentials = credentials_for(account)
    login_url = login_url or account_login_url(account)

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise RuntimeError("selenium is not installed. Run: pip install -r requirements.txt") from exc

    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    driver = None
    try:
        chrome_options = Options()
        if os.getenv("KITE_AUTO_LOGIN_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1280,720")

        logger.info("Starting automated Kite login for %s", account.label)
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(30)
        wait = WebDriverWait(driver, 15)

        driver.get(login_url)
        raise_if_kite_error_page(driver)

        user_id_field = wait_for_field(
            driver,
            wait,
            ["input#userid", "input[name='userid']", "input[type='text']"],
            purpose="user ID",
        )
        fill_field(driver, user_id_field, credentials.user_id)

        password_field = wait_for_field(
            driver,
            wait,
            ["input#password", "input[name='password']", "input[type='password']"],
            purpose="password",
        )
        fill_field(driver, password_field, credentials.password)

        click_submit(driver, wait)

        totp_field = find_totp_field(driver, wait)
        fill_field(driver, totp_field, generate_totp(credentials.totp_secret))

        try:
            time.sleep(1)
            click_submit(driver, wait, required=False)
        except Exception:
            pass

        request_token = wait_for_request_token(driver)
        if not request_token:
            raise RuntimeError(f"Timed out waiting for request_token. Current URL: {driver.current_url}")

        logger.info("Automated Kite login succeeded for %s", account.label)
        return request_token
    except Exception as exc:
        debug_path = save_debug_artifacts(driver, account.label) if driver else None
        message = str(exc).strip() or exc.__class__.__name__
        if debug_path:
            message = f"{message}. Saved Selenium debug files to {debug_path}"
        raise RuntimeError(message) from exc
    finally:
        if driver:
            driver.quit()


def account_login_url(account: Account) -> str:
    from kite_core import kite_for

    return kite_for(account).login_url()


def find_totp_field(driver, wait):
    from selenium.webdriver.common.by import By

    selectors = [
        "input[type='number']",
        "input[autocomplete='one-time-code']",
        "input[label='External TOTP']",
        "input.su-input-field",
        "input[type='text']",
    ]
    wait.until(EC_any_visible_input(selectors))

    for selector in selectors:
        fields = driver.find_elements(By.CSS_SELECTOR, selector)
        for field in fields:
            field_id = (field.get_attribute("id") or "").lower()
            field_name = (field.get_attribute("name") or "").lower()
            field_type = (field.get_attribute("type") or "").lower()
            if field_type == "password":
                continue
            if is_login_user_field(field, field_id, field_name, field_type):
                continue
            if field.is_displayed() and field.is_enabled():
                return field

    raise RuntimeError("Could not find the Kite TOTP input field.")


def wait_for_field(driver, wait, selectors: list[str], purpose: str):
    from selenium.webdriver.common.by import By

    wait.until(EC_any_visible_input(selectors, allow_login_fields=True))
    for selector in selectors:
        for field in driver.find_elements(By.CSS_SELECTOR, selector):
            if field.is_displayed() and field.is_enabled():
                return field
    raise RuntimeError(f"Could not find the Kite {purpose} field.")


def raise_if_kite_error_page(driver) -> None:
    from selenium.webdriver.common.by import By

    pre_elements = driver.find_elements(By.CSS_SELECTOR, "pre")
    for element in pre_elements:
        text = element.text.strip()
        if not text:
            continue
        try:
            payload = loads(text)
        except JSONDecodeError:
            continue
        if payload.get("status") == "error":
            message = payload.get("message") or "Kite rejected the login page."
            error_type = payload.get("error_type")
            if error_type:
                message = f"{message} ({error_type})"
            raise RuntimeError(message)


def fill_field(driver, field, value: str) -> None:
    from selenium.webdriver.common.keys import Keys

    try:
        field.click()
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(value)
        return
    except Exception:
        pass

    driver.execute_script(
        """
        const input = arguments[0];
        const value = arguments[1];
        input.focus();
        input.value = value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        field,
        value,
    )


def click_submit(driver, wait, required: bool = True) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC

    selectors = [
        "button[type='submit']",
        "button.button-orange",
        "button.su-button",
        "input[type='submit']",
    ]
    for selector in selectors:
        try:
            button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            button.click()
            return True
        except Exception:
            continue

    if required:
        raise RuntimeError("Could not find a clickable Kite submit button.")
    return False


def wait_for_request_token(driver) -> str:
    from selenium.webdriver.common.by import By

    for _ in range(30):
        time.sleep(1)
        current_url = driver.current_url
        if "request_token" in current_url:
            params = parse_qs(urlparse(current_url).query)
            request_token = params.get("request_token", [""])[0]
            if request_token:
                return request_token

        for selector in [".error", ".error-message", ".status-message.error", ".su-message"]:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = element.text.strip()
                if element.is_displayed() and text:
                    raise RuntimeError(f"Kite login failed: {text}")

    return ""


def save_debug_artifacts(driver, label: str) -> Path:
    safe_label = env_suffix(label).lower()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = DATA_DIR / "selenium_debug" / f"{stamp}_{safe_label}"
    debug_dir.mkdir(parents=True, exist_ok=True)

    (debug_dir / "url.txt").write_text(driver.current_url, encoding="utf-8")
    (debug_dir / "page.html").write_text(driver.page_source, encoding="utf-8")
    try:
        driver.save_screenshot(str(debug_dir / "screenshot.png"))
    except Exception:
        pass
    return debug_dir


class EC_any_visible_input:
    def __init__(self, selectors: list[str], allow_login_fields: bool = False):
        self.selectors = selectors
        self.allow_login_fields = allow_login_fields

    def __call__(self, driver):
        from selenium.webdriver.common.by import By

        for selector in self.selectors:
            for field in driver.find_elements(By.CSS_SELECTOR, selector):
                field_id = (field.get_attribute("id") or "").lower()
                field_name = (field.get_attribute("name") or "").lower()
                field_type = (field.get_attribute("type") or "").lower()
                if (
                    not self.allow_login_fields
                    and is_login_user_field(field, field_id, field_name, field_type)
                ):
                    continue
                if not self.allow_login_fields and field_type == "password":
                    continue
                if field.is_displayed() and field.is_enabled():
                    return field
        return False


def is_login_user_field(field, field_id: str, field_name: str, field_type: str) -> bool:
    if field_id != "userid" and field_name != "userid":
        return False

    label = (field.get_attribute("label") or "").lower()
    placeholder = (field.get_attribute("placeholder") or "").lower()
    max_length = field.get_attribute("maxlength") or ""
    code_markers = ["totp", "code", "otp", "pin"]
    if field_type == "number" or max_length == "6":
        return False
    if any(marker in label or marker in placeholder for marker in code_markers):
        return False
    return True
