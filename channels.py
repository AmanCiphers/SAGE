import os
import threading
import time


class Channel:
    def send(self, text):
        raise NotImplementedError

    def start(self):
        pass


class ConsoleChannel(Channel):
    def send(self, text):
        print(f"\n[sage] {text}\n")


class TelegramChannel(Channel):
    def __init__(self, token):
        import telegram

        self.bot = telegram.Bot(token=token)
        self.offset = 0
        self.target_chat = None
        self.on_message = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()
        print("[telegram] listening for messages")

    def _poll(self):
        while self._running:
            try:
                updates = self.bot.get_updates(offset=self.offset, timeout=30)
                for u in updates:
                    self.offset = u.update_id + 1
                    msg = (u.message or u.edited_message) if not u.callback_query else None
                    text = msg.text if msg else (u.callback_query.data if u.callback_query else None)
                    chat_id = (msg or u.callback_query).chat.id if msg or u.callback_query else None
                    if text and chat_id:
                        self.target_chat = chat_id
                        if self.on_message:
                            self.on_message(text)
            except Exception:
                time.sleep(5)

    def send(self, text):
        if not self.target_chat:
            return False
        self.bot.send_message(chat_id=self.target_chat, text=text)
        return True


class WhatsAppChannel(Channel):
    def __init__(self, token, phone_id, to_number):
        import requests

        self.requests = requests
        self.token = token
        self.phone_id = phone_id
        self.to_number = to_number

    def send(self, text):
        resp = self.requests.post(
            f"https://graph.facebook.com/v19.0/{self.phone_id}/messages",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "messaging_product": "whatsapp",
                "to": self.to_number,
                "type": "text",
                "text": {"body": text},
            },
            timeout=30,
        )
        return resp.status_code == 200


def build_channels():
    channels = [ConsoleChannel()]
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        try:
            channels.append(TelegramChannel(tok))
        except Exception as e:
            print(f"[telegram] disabled: {e} (pip install python-telegram-bot)")
    wa = os.environ.get("WHATSAPP_TOKEN")
    if wa and os.environ.get("WHATSAPP_PHONE_ID") and os.environ.get("WHATSAPP_TO"):
        channels.append(WhatsAppChannel(wa, os.environ["WHATSAPP_PHONE_ID"], os.environ["WHATSAPP_TO"]))
    return channels


def start_channels(channels):
    for c in channels:
        c.start()


def notify(channels, text):
    for c in channels:
        try:
            c.send(text)
        except Exception as e:
            print(f"[notify] {type(c).__name__}: {e}")