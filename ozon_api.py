"""Клиент Ozon Seller API: запросы, повторы, ограничение частоты, журнал."""
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

BASE_URL = "https://api-seller.ozon.ru"

# Минимальный интервал между вызовами метода, сек.
# Создание черновиков и заявок — не больше 2 раз в минуту.
MIN_INTERVAL = {
    "/v1/draft/crossdock/create": 31,
    "/v1/draft/direct/create": 31,
    "/v1/draft/multi-cluster/create": 31,
    "/v2/draft/supply/create": 31,
}
DEFAULT_INTERVAL = 0.6        # один и тот же метод — не чаще ~1,5 раза в секунду
GLOBAL_INTERVAL = 0.22        # все запросы вместе — не чаще ~4–5 в секунду (лимит Озона на продавца)


class OzonError(Exception):
    def __init__(self, message, status=None, path=None, body=None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body


class OzonClient:
    def __init__(self, client_id, api_key, log=None, demo=None, speed=1.0):
        self.client_id = str(client_id or "").strip()
        self.api_key = str(api_key or "").strip()
        self.log = log            # функция log(str) для журнала запросов
        self.demo = demo          # объект демо-Озона (mock) вместо сети
        self.speed = speed        # множитель интервалов (для демо — меньше)
        self._last = {}
        self._lock = threading.Lock()
        self.stop_flag = None     # threading.Event — прервать ожидание

    # ---------------------------------------------------------------
    def time_until(self, path):
        """Сколько секунд осталось до момента, когда метод можно вызвать без ожидания."""
        interval = MIN_INTERVAL.get(path, DEFAULT_INTERVAL) * self.speed
        with self._lock:
            return max(0.0, self._last.get(path, 0) + interval - time.time())

    def _wait_turn(self, path):
        interval = MIN_INTERVAL.get(path, DEFAULT_INTERVAL) * self.speed
        with self._lock:
            now = time.time()
            ready = max(self._last.get(path, 0) + interval, self._last.get("*", 0) + GLOBAL_INTERVAL * self.speed)
            self._last["*"] = max(now, ready)
            delay = max(0.0, ready - now)
            self._last[path] = now + delay
        if delay:
            self.sleep(delay)

    def sleep(self, sec):
        if self.stop_flag is not None:
            if self.stop_flag.wait(sec):
                raise OzonError("Остановлено пользователем")
        else:
            time.sleep(sec)

    def _log(self, s):
        if self.log:
            try:
                self.log(s)
            except Exception:
                pass

    def _raw(self, path, body):
        if self.demo is not None:
            return self.demo.handle(path, body)
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(BASE_URL + path, data=data, method="POST", headers={
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
                raw = r.read()
                return r.status, json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                js = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                js = {"message": raw[:500].decode("utf-8", "replace")}
            return e.code, js

    def post(self, path, body=None, retries=8):
        attempt = 0
        while True:
            attempt += 1
            self._wait_turn(path)
            try:
                status, js = self._raw(path, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                self._log(f"{path} → сеть: {e}")
                if attempt >= retries:
                    raise OzonError(f"Нет связи с Озоном: {e}", path=path)
                self.sleep(min(5 * attempt, 30) * self.speed)
                continue
            self._log(f"POST {path} {json.dumps(body, ensure_ascii=False)[:2000]}\n  ← {status} "
                      f"{json.dumps(js, ensure_ascii=False)[:3000]}")
            if status == 429:
                per_second = "per second" in str(js.get("message", "")).lower() if isinstance(js, dict) else False
                if per_second:
                    # секундный лимит: хватает короткой паузы (раньше ждали 20 с — отсюда медленные этикетки)
                    if attempt >= 20:
                        raise OzonError("Озон: превышен лимит запросов в секунду (429)", 429, path, js)
                    self.sleep(min(0.8 * attempt, 4) * self.speed)
                    continue
                if attempt >= retries:
                    raise OzonError("Озон: превышен лимит запросов (429)", 429, path, js)
                self.sleep((20 if attempt < 3 else 60) * self.speed)
                continue
            if status >= 500:
                if attempt >= min(retries, 4):
                    raise OzonError(f"Озон: ошибка сервера {status}: {_msg(js)}", status, path, js)
                self.sleep(5 * attempt * self.speed)
                continue
            if status >= 400:
                raise OzonError(f"Озон {status}: {_msg(js)}", status, path, js)
            return js

    def get_file(self, url):
        """Скачивает файл (PDF этикеток) по ссылке из ответа Озона."""
        if self.demo is not None:
            return self.demo.get_file(url)
        last_err = None
        for with_auth in ((True, False) if "ozon.ru" in url else (False, True)):
            headers = {"Client-Id": self.client_id, "Api-Key": self.api_key} if with_auth else {}
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in (401, 403, 400):
                    break
            except Exception as e:
                last_err = e
                break
        raise OzonError(f"Не удалось скачать файл этикеток: {last_err}")

    def poll(self, path, body, done, interval=2.0, timeout=180):
        """Опрашивает метод статуса, пока done(ответ) не вернёт True."""
        t0 = time.time()
        while True:
            js = self.post(path, body)
            if done(js):
                return js
            if time.time() - t0 > timeout:
                raise OzonError(f"Озон долго не отвечает ({path}), попробуйте «Продолжить» позже")
            self.sleep(interval * self.speed)


def _msg(js):
    if isinstance(js, dict):
        m = js.get("message") or js.get("error") or ""
        det = js.get("details")
        if det and isinstance(det, list):
            m += " " + "; ".join(str(d.get("value") or d) for d in det if d)
        return m.strip() or json.dumps(js, ensure_ascii=False)[:300]
    return str(js)[:300]
