"""Entorno aislado para las pruebas del backend.

Cada ejecución usa bases SQLite nuevas en un directorio temporal y apaga el relay
y el worker (SNS_ENABLED / SQS_ENABLED): las pruebas llaman a sus funciones
directamente, con un publicador falso en lugar de LocalStack.

Tiene que configurarse antes de importar cualquier servicio, porque cada uno
abre su base al importarse.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_DIR"] = tempfile.mkdtemp(prefix="jobbi-tests-")
os.environ.pop("DATABASE_URL", None)
os.environ["SNS_ENABLED"] = "false"
os.environ["SQS_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
