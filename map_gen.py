def axial_ring(radius):
    cells = []
    for q in range(-radius, radius + 1):
        for r in range(-radius, radius + 1):
            s = -q - r
            if abs(s) <= radius:
                cells.append((q, r))
    return cells

def generate_map(radius):
    cells = {}
    id_counter = 1
    for q, r in axial_ring(radius):
        cells[id_counter] = {
            "id": id_counter,
            "q": q,
            "r": r,
            "owner": None,
            "troops": 0
        }
        id_counter += 1
    return cells
