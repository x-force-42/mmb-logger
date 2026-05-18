"""Backfill heurístico de colunas derivadas em ciclos pré-T1.

Sub-pacote pra one-shots de migração retroativa que não cabem no
reconciler (idempotente, mas heurístico — não a partir do canon do
método). Contraponto a `reconcile/`: o reconciler projeta o estado
canônico atual; o backfill preenche o gap histórico.
"""
