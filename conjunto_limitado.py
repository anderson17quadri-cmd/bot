"""
conjunto_limitado.py  -  Um "set" com tamanho maximo (para dedup em 24/7)
============================================================================
Varios sitios do bot guardam um set() de coisas "ja vistas" (pool_address,
mints, pares carteira|mint) para nao repetir alertas/compras/sinais. Um
set() normal cresce para sempre - correndo 24/7, isso e uma fuga de
memoria lenta mas real.

Esta classe comporta-se como um set() (operador 'in', .add()) mas nunca
ultrapassa 'capacidade': ao exceder, descarta a entrada mais ANTIGA
(FIFO) - a mesma logica em toda a parte que faz "ja vi isto?" seguido de
"marca como visto".
"""

from collections import deque


class ConjuntoLimitado:
    def __init__(self, capacidade: int = 5000):
        self.capacidade = capacidade
        self._itens: set = set()
        self._ordem: deque = deque()  # mesma ordem de insercao (FIFO)

    def __contains__(self, chave) -> bool:
        return chave in self._itens

    def __len__(self) -> int:
        return len(self._itens)

    def add(self, chave) -> None:
        if chave in self._itens:
            return
        self._itens.add(chave)
        self._ordem.append(chave)
        if len(self._itens) > self.capacidade:
            mais_antigo = self._ordem.popleft()
            self._itens.discard(mais_antigo)
