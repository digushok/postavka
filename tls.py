"""Защищённые соединения с Озоном и GitHub.

Сертификаты проверяются так же, как это делает браузер: через системное хранилище
(в Windows — хранилище сертификатов Windows). Поэтому программа работает на компьютерах,
где HTTPS проверяет антивирус (Касперский, ESET, Dr.Web) или стоит корпоративный прокси,
а также там, где установлены российские корневые сертификаты.
Проверка сертификатов никогда не отключается.
"""
import os
import socket
import ssl
import tempfile

_CTX = None


def context():
    global _CTX
    if _CTX is None:
        try:
            import truststore
            _CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _CTX = ssl.create_default_context()
    return _CTX


def is_cert_error(err):
    reason = getattr(err, "reason", err)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(err)


def who_intercepts(host, port=443, timeout=8):
    """Чей сертификат на самом деле приходит вместо сертификата сайта.

    Соединение открывается только чтобы прочитать имя издателя сертификата:
    никакие запросы и данные (в том числе API-ключ) по нему не отправляются."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                der = s.getpeercert(binary_form=True)
        pem = ssl.DER_cert_to_PEM_cert(der)
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pem)
            info = ssl._ssl._test_decode_cert(path)      # стандартный разбор сертификата Python
        finally:
            os.remove(path)
        issuer = dict(x[0] for x in info.get("issuer", ()))
        return issuer.get("organizationName") or issuer.get("commonName") or ""
    except Exception:
        return ""


def explain(host, err, port=443):
    """Понятное объяснение ошибки сертификата для пользователя."""
    who = who_intercepts(host, port)
    head = f"Не удаётся проверить сертификат {host}"
    who_txt = f": соединение подменяет «{who}»" if who else ""
    return (f"{head}{who_txt}. Обычно так делает антивирус с проверкой HTTPS или прокси. "
            f"Что сделать: в антивирусе добавьте {host} в исключения проверки защищённых соединений "
            f"(или исключите программу «Поставки Ozon»); если это рабочий компьютер — попросите системного "
            f"администратора добавить корневой сертификат прокси в Windows. Техническая причина: {err}")
