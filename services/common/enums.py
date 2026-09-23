"""Enumeraciones del modelo de dominio JOBBI.

Viven en `common` porque son parte del lenguaje ubicuo compartido entre los
contextos delimitados. Cada servicio importa únicamente las que necesita: la
frontera se mantiene en los datos y en el despliegue, no en el vocabulario.
"""

from enum import Enum


class EstadoCuenta(str, Enum):
    ACTIVO = "ACTIVO"
    SUSPENDIDO = "SUSPENDIDO"
    INACTIVO = "INACTIVO"


class EstadoVerificacion(str, Enum):
    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


class EstadoContratacion(str, Enum):
    SOLICITADA = "SOLICITADA"
    ACEPTADA = "ACEPTADA"
    EN_CURSO = "EN_CURSO"
    CHECK_IN = "CHECK_IN"
    CHECK_OUT = "CHECK_OUT"
    COMPLETADA = "COMPLETADA"
    CANCELADA = "CANCELADA"
    EN_DISPUTA = "EN_DISPUTA"


class MedioPago(str, Enum):
    EFECTIVO = "EFECTIVO"
    PLATAFORMA = "PLATAFORMA"


class PlanPrestador(str, Enum):
    FREE = "FREE"
    PRO = "PRO"


class EstadoIncidente(str, Enum):
    ABIERTO = "ABIERTO"
    EN_INVESTIGACION = "EN_INVESTIGACION"
    RESUELTO = "RESUELTO"
    ESCALADO = "ESCALADO"


class EstadoModeracion(str, Enum):
    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


def valores(enumeracion: type[Enum]) -> list[str]:
    """Lista de valores de una enumeración, para exponerla como catálogo."""
    return [miembro.value for miembro in enumeracion]
