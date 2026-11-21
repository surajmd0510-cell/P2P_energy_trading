"""Thin PayPal REST client (Orders v2, Payouts, webhook verification)."""
import base64, os, time, requests


class PayPalError(Exception):
    pass


class PayPal:
    def __init__(self):
        live = os.environ.get("PAYPAL_ENV", "sandbox") == "live"
        self.base = "https://api-m.paypal.com" if live else "https://api-m.sandbox.paypal.com"
        self._tok, self._exp = None, 0

    def _auth(self):
        if self._tok and time.time() < self._exp - 30:
            return self._tok
        cid, sec = os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]
        r = requests.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                          auth=(cid, sec), timeout=20)
        if r.status_code != 200:
            raise PayPalError(f"auth failed: {r.text}")
        j = r.json()
        self._tok, self._exp = j["access_token"], time.time() + j["expires_in"]
        return self._tok

    def _call(self, method, path, body=None, request_id=None):
        h = {"Authorization": f"Bearer {self._auth()}", "Content-Type": "application/json"}
        if request_id:
            h["PayPal-Request-Id"] = request_id  # idempotency key
        r = requests.request(method, self.base + path, json=body, headers=h, timeout=30)
        if r.status_code >= 300:
            raise PayPalError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json() if r.text else {}

    # ---- Checkout (Orders v2) -------------------------------------------------
    def create_order(self, minor, currency, ref, return_url, cancel_url):
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": ref, "custom_id": ref,
                                    "amount": {"currency_code": currency, "value": f"{minor / 100:.2f}"}}],
                "payment_source": {"paypal": {"experience_context": {
                    "return_url": return_url, "cancel_url": cancel_url, "user_action": "PAY_NOW"}}}}
        o = self._call("POST", "/v2/checkout/orders", body, request_id=f"order-{ref}")
        link = next((l["href"] for l in o.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return o["id"], link

    def capture_order(self, order_id):
        o = self._call("POST", f"/v2/checkout/orders/{order_id}/capture", {}, request_id=f"cap-{order_id}")
        return o.get("status") == "COMPLETED"

    # ---- Payouts --------------------------------------------------------------
    def payout(self, batch_id, currency, items):
        body = {"sender_batch_header": {"sender_batch_id": batch_id,  # unique => no double pay on retry
                                        "email_subject": "SolarPool energy settlement",
                                        "email_message": "Your community energy earnings have been paid."},
                "items": [{"recipient_type": "EMAIL", "receiver": i["email"], "sender_item_id": i["id"],
                           "amount": {"value": f"{i['amount'] / 100:.2f}", "currency": currency},
                           "note": "Energy credits sold to neighbours"} for i in items]}
        r = self._call("POST", "/v1/payments/payouts", body, request_id=batch_id)
        return r["batch_header"]["payout_batch_id"]

    def get_payout(self, pp_batch_id):
        return self._call("GET", f"/v1/payments/payouts/{pp_batch_id}")

    # ---- Webhooks -------------------------------------------------------------
    def verify_webhook(self, headers, event):
        body = {"auth_algo": headers.get("PAYPAL-AUTH-ALGO"), "cert_url": headers.get("PAYPAL-CERT-URL"),
                "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
                "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
                "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
                "webhook_id": os.environ["PAYPAL_WEBHOOK_ID"], "webhook_event": event}
        return self._call("POST", "/v1/notifications/verify-webhook-signature", body) \
                   .get("verification_status") == "SUCCESS"


_pp = None


def pp():
    global _pp
    if _pp is None:
        _pp = PayPal()
    return _pp
