from sqlalchemy import func, select

from ontara import db, demo


def test_precarga_todos_los_reportes_una_sola_vez():
    motor = db.crear_motor("sqlite://")
    assert demo.precargar(motor) == len(demo.ORDEN)
    with motor.connect() as cx:
        assert cx.execute(select(func.count()).select_from(db.cargas)).scalar() == len(demo.ORDEN)
        assert cx.execute(select(func.count()).select_from(db.decisiones)).scalar() > 0
    assert demo.precargar(motor) == 0
