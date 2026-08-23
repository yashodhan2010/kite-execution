from __future__ import annotations

from io import BytesIO
from urllib.parse import urljoin

import pandas as pd
import requests

from kite_core import normalize_target_portfolio


class VrikshaClient:
    def __init__(self, base_url: str, cookie: str = "", bearer_token: str = "") -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        if cookie:
            self.session.headers["Cookie"] = cookie
        if bearer_token:
            self.session.headers["Authorization"] = f"Bearer {bearer_token}"

    def subscriptions(self) -> list[dict]:
        response = self.session.get(
            self.url("/api/execution/subscriptions"),
            timeout=20,
        )
        self.raise_for_status(response)
        data = response.json()
        if isinstance(data, dict) and "subscriptions" in data:
            return data["subscriptions"]
        if isinstance(data, list):
            return data
        raise ValueError("Unexpected Vriksha subscriptions response.")

    def latest_model_portfolio(self, strategy_id: str) -> pd.DataFrame:
        return self.fetch_csv(
            f"/api/execution/strategies/{strategy_id}/latest-model-portfolio.csv"
        )

    def rebalance_history(self, strategy_id: str) -> pd.DataFrame:
        return self.fetch_csv(
            f"/api/execution/strategies/{strategy_id}/rebalance-history.csv"
        )

    def fetch_csv(self, path: str) -> pd.DataFrame:
        response = self.session.get(self.url(path), timeout=30)
        self.raise_for_status(response)
        return normalize_target_portfolio(BytesIO(response.content))

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @staticmethod
    def raise_for_status(response: requests.Response) -> None:
        if response.ok:
            return
        message = response.text.strip() or response.reason
        if response.status_code == 401:
            raise PermissionError("Vriksha login required.")
        if response.status_code == 403:
            raise PermissionError("Vriksha subscription does not allow this strategy.")
        raise RuntimeError(f"Vriksha request failed ({response.status_code}): {message}")
