"""
telegram_alerts.py  -  Notificacoes via Telegram Bot API (opcional)
=======================================================================
Envia mensagens curtas para um chat do Telegram quando algo relevante
acontece (compra, venda, erro grave, mudanca significativa de saldo).

Como configurar (gratuito):
  1. No Telegram, fala com o @BotFather -> /newbot -> segue os passos.
     Ele devolve um TOKEN (ex: 123456:ABC-DEF...).
  2. Manda uma mensagem qualquer ao teu bot novo (para ele "saber" de ti).
  3. Vai a https://api.telegram.org/bot<TOKEN>/getUpdates e le o
     "chat":{"id": ...} da tua mensagem - esse numero e o TELEGRAM_CHAT_ID.
  4. Poe TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID e TELEGRAM_ALERTAS_ATIVO=true
     no .env.

Se nao estiver configurado (ou ativo=false), enviar() e um no-op - o bot
funciona exatamente igual, sem notificacoes. NUNCA levanta excecao para
fora: uma falha de rede aqui nunca deve derrubar o bot nem interromper
um ciclo de trading.
"""

import requests

import config

_URL_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def esta_configurado() -> bool:
    """True se ha token+chat_id no .env E o toggle esta ligado."""
    return bool(
        config.TELEGRAM_ALERTAS_ATIVO
        and config.TELEGRAM_BOT_TOKEN
        and config.TELEGRAM_CHAT_ID
    )


def enviar(texto: str) -> bool:
    """Manda 'texto' para o chat configurado. Devolve True se (aparentemente)
    foi enviado, False caso contrario - NUNCA levanta excecao, o pior que
    acontece e a notificacao nao chegar (o bot continua na mesma)."""
    if not esta_configurado():
        return False
    try:
        url = _URL_BASE.format(token=config.TELEGRAM_BOT_TOKEN)
        resposta = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": texto,
            "disable_web_page_preview": True,
        }, timeout=10)
        return resposta.status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# Teste rapido:  python3 telegram_alerts.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    if not esta_configurado():
        print("Telegram NAO configurado (falta TELEGRAM_BOT_TOKEN/CHAT_ID, ou TELEGRAM_ALERTAS_ATIVO=false).")
    else:
        ok = enviar("🤖 Teste do Sniper Bot - se vires isto, o Telegram esta a funcionar!")
        print("Enviado com sucesso!" if ok else "Falhou a enviar (confirma o token/chat_id).")
