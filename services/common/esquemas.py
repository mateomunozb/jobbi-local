"""Base de los esquemas de respuesta (Pydantic).

`from_attributes` permite construirlos directamente desde una fila de
SQLAlchemy, de modo que el repositorio devuelve los mismos tipos que ya
declaraban los endpoints y el contrato de la API no cambió al pasar de listas
en memoria a base de datos.
"""

from pydantic import BaseModel, ConfigDict


class Esquema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
