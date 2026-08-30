"""Harness conversacional: determinista primero, LLM ultimo.

La regla del modulo: un turno solo llega a la API de Anthropic si ninguna
capa anterior pudo resolverlo. Todo lo que caiga en el router de patrones
(app.harness.router.etapa1) es gratis para siempre.

El LLM no se usa como router ni como renderer. Las respuestas de consulta
salen de plantillas en app.harness.render.
"""
from app.harness.intents import Intent, Result
from app.harness.router import rutear

__all__ = ["Intent", "Result", "rutear"]
