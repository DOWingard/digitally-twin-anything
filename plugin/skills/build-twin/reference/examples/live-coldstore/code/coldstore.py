"""The cold store's own code: the policy of its setpoint loop.

`policy` is called with a read-only view of the twin (`twin.get(path, **params)`
returns the JSON document of `GET /api/v1<path>`) and gives back the actions it
proposes. Each proposal is judged by the twin against the control's bounds and
autonomy before anything is sent.
"""

TOO_WARM_C = -16.0
STEP_C = 1.0


def policy(twin):
    rooms = twin.get("/objects/Room", columns="temperature,setpoint")["data"]
    actions = []
    for room in rooms:
        temperature, setpoint = room.get("temperature"), room.get("setpoint")
        if temperature is None or setpoint is None:
            continue
        if temperature > TOO_WARM_C and temperature > setpoint + STEP_C:
            actions.append(
                {
                    "action": "set_setpoint",
                    "object": room["uid"],
                    "params": {"value": setpoint - STEP_C},
                    "reason": f"air at {temperature} °C is above the setpoint {setpoint} °C",
                }
            )
    return {"actions": actions, "notes": [f"{len(rooms)} room(s) looked at"]}
