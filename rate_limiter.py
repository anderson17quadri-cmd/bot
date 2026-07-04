"""
rate_limiter.py  -  Limitador simples de pedidos por segundo (token bucket)
=============================================================================
Serve para nunca ultrapassarmos o limite de pedidos do plano gratuito do
RPC (Helius) quando fazemos chamadas extra (ex: a partir do detector
websocket, que ao ver um token novo pode precisar de ler a curva).

Modelo "token bucket": ha um balde com uma capacidade maxima de fichas;
cada pedido gasta 1 ficha; o balde reenche a uma taxa constante (X fichas
por segundo). Se nao houver fichas, o pedido espera o tempo necessario -
assim a media nunca passa de X pedidos/segundo, mas permite pequenas
rajadas ate a capacidade do balde.

Thread-safe (usa um Lock), porque o websocket corre numa thread propria.
"""

import threading
import time


class RateLimiter:
    def __init__(self, pedidos_por_segundo: float, capacidade: float | None = None):
        self.taxa = max(0.1, pedidos_por_segundo)      # fichas que entram por segundo
        self.capacidade = capacidade or self.taxa      # tamanho maximo do balde
        self._fichas = self.capacidade                 # comeca cheio
        self._ultimo = time.monotonic()
        self._lock = threading.Lock()

    def adquirir(self, timeout: float | None = None) -> bool:
        """Espera ate haver uma ficha e gasta-a. Devolve True quando
        consegue; False se 'timeout' segundos passarem sem conseguir."""
        inicio = time.monotonic()
        while True:
            with self._lock:
                agora = time.monotonic()
                # Reenche o balde consoante o tempo que passou
                self._fichas = min(
                    self.capacidade,
                    self._fichas + (agora - self._ultimo) * self.taxa,
                )
                self._ultimo = agora
                if self._fichas >= 1:
                    self._fichas -= 1
                    return True
                # Quanto falta esperar ate haver 1 ficha
                falta = (1 - self._fichas) / self.taxa
            if timeout is not None and (time.monotonic() - inicio + falta) > timeout:
                return False
            time.sleep(min(falta, 0.25))
