-- Дописування події до рейсу одним викликом.
-- Рейс блокується через FOR UPDATE, потім береться max(seq)+1 і робиться INSERT,
-- тому одночасні дописування не конфліктують. Для fuel і incident пишеться ще й окрема таблиця.
CREATE OR REPLACE FUNCTION bench.append_trip_event(
    p_trip_number text, p_type bench.trip_event_type, p_at timestamptz,
    p_odometer int, p_location text, p_minutes int, p_reason text, p_receiver text,
    p_liters numeric, p_unit_price numeric, p_station text,
    p_incident_type bench.incident_type, p_description text, p_amount numeric, p_at_fault boolean)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    v_trip bigint; v_veh int; v_drv int; v_seq int;
BEGIN
    SELECT trip_id, vehicle_id, driver_id INTO v_trip, v_veh, v_drv
      FROM bench.trips WHERE trip_number = p_trip_number FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Рейс % не знайдено', p_trip_number;
    END IF;
    SELECT coalesce(max(seq), 0) + 1 INTO v_seq FROM bench.trip_events WHERE trip_id = v_trip;
    INSERT INTO bench.trip_events (trip_id, seq, type, occurred_at, odometer_km, location,
                                   duration_min, reason, receiver_name)
    VALUES (v_trip, v_seq, p_type, p_at, p_odometer, p_location, p_minutes, p_reason, p_receiver);
    IF p_type = 'fuel' THEN
        INSERT INTO bench.fuel_transactions (trip_id, event_seq, vehicle_id, driver_id, filled_at,
                                             quantity, unit_price, odometer_km, station)
        VALUES (v_trip, v_seq, v_veh, v_drv, p_at, p_liters, p_unit_price, p_odometer, p_station);
    ELSIF p_type = 'incident' THEN
        INSERT INTO bench.incidents (trip_id, event_seq, vehicle_id, driver_id, type, occurred_at,
                                     description, amount, driver_at_fault)
        VALUES (v_trip, v_seq, v_veh, v_drv, p_incident_type, p_at, p_description, p_amount, p_at_fault);
    END IF;
    RETURN v_seq;
END $$;
