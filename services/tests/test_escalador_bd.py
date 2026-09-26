"""Política del escalador de la base (simulación de Aurora Serverless v2, Fallo 3)."""

from escalador_bd.politica import BAJAR, MANTENER, NIVELES, SUBIR, Politica

MI = 2**20


class Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _politica():
    reloj = Reloj()
    return Politica(reloj=reloj), reloj


def test_sube_solo_si_la_cpu_sigue_alta_dos_lecturas_seguidas():
    politica, _ = _politica()
    # 0,5 ACU = 0,5 núcleos: 0,45 es el 90 %.
    assert politica.decidir(0, 0.45, 200 * MI)[0] == MANTENER
    assert politica.decidir(0, 0.45, 200 * MI)[0] == SUBIR


def test_un_pico_aislado_no_sube():
    politica, _ = _politica()
    politica.decidir(0, 0.45, 200 * MI)
    politica.decidir(0, 0.10, 200 * MI)
    assert politica.decidir(0, 0.45, 200 * MI)[0] == MANTENER


def test_espera_el_efecto_de_una_subida_antes_de_la_siguiente():
    politica, reloj = _politica()
    politica.cambio_aplicado()
    reloj.t += 5
    politica.decidir(1, 0.9, 200 * MI)
    assert politica.decidir(1, 0.9, 200 * MI)[0] == MANTENER
    reloj.t += 15
    assert politica.decidir(1, 0.9, 200 * MI)[0] == SUBIR


def test_sube_por_memoria_aunque_la_cpu_este_tranquila():
    politica, _ = _politica()
    politica.decidir(0, 0.05, 600 * MI)  # 600 de 640 Mi = 94 %
    decision, motivo = politica.decidir(0, 0.05, 600 * MI)
    assert decision == SUBIR and "memoria" in motivo


def test_en_la_capacidad_maxima_no_sube_mas():
    politica, _ = _politica()
    ultimo = len(NIVELES) - 1
    for _ in range(3):
        decision, _motivo = politica.decidir(ultimo, 3.9, 500 * MI)
    assert decision == MANTENER


def test_baja_solo_tras_30_s_de_uso_bajo():
    politica, reloj = _politica()
    assert politica.decidir(3, 0.05, 150 * MI)[0] == MANTENER
    reloj.t += 20
    assert politica.decidir(3, 0.05, 150 * MI)[0] == MANTENER
    reloj.t += 10
    assert politica.decidir(3, 0.05, 150 * MI)[0] == BAJAR


def test_no_baja_si_el_uso_no_cabe_holgado_en_el_nivel_inferior():
    politica, reloj = _politica()
    # En 4 ACU usa 1,4 núcleos (35 %): el nivel inferior tiene 2, quedaría al 70 %.
    politica.decidir(3, 1.4, 300 * MI)
    reloj.t += 60
    assert politica.decidir(3, 1.4, 300 * MI)[0] == MANTENER


def test_no_baja_si_la_memoria_usada_no_cabe_en_el_nivel_inferior():
    politica, reloj = _politica()
    # CPU baja, pero 900 Mi no caben con holgura en los 1024 Mi de 1 ACU.
    politica.decidir(2, 0.05, 900 * MI)
    reloj.t += 60
    assert politica.decidir(2, 0.05, 900 * MI)[0] == MANTENER


def test_tras_bajar_vuelve_a_esperar_antes_del_siguiente_escalon():
    politica, reloj = _politica()
    politica.decidir(3, 0.05, 150 * MI)
    reloj.t += 30
    assert politica.decidir(3, 0.05, 150 * MI)[0] == BAJAR
    politica.cambio_aplicado()
    reloj.t += 5
    assert politica.decidir(2, 0.05, 150 * MI)[0] == MANTENER


def test_en_la_capacidad_minima_se_queda():
    politica, reloj = _politica()
    politica.decidir(0, 0.01, 100 * MI)
    reloj.t += 120
    assert politica.decidir(0, 0.01, 100 * MI)[0] == MANTENER


def test_la_escalera_dobla_la_cpu_y_nunca_baja_la_memoria_al_subir():
    for inferior, superior in zip(NIVELES, NIVELES[1:]):
        assert superior.cpu_limite_m == 2 * inferior.cpu_limite_m
        assert superior.memoria_limite_mi > inferior.memoria_limite_mi
        assert superior.work_mem_mb > inferior.work_mem_mb
        assert superior.cpu_reserva_m <= superior.cpu_limite_m
