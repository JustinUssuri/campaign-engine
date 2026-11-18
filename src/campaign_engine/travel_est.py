"""地面旅行速度与耗时离线估算器（读取 rules/travel.yaml）。"""
import argparse
from fractions import Fraction
import json

import yaml

from .store import ROOT, pkg_data


def calculate(*, species: str = 'human', load_ratio: float = 0.25, road: str = '好', miles: float = 30.0) -> dict:
    src = ROOT / 'rules/travel.yaml'
    if not src.exists():
        src = pkg_data('rules', 'travel.yaml')
    rules = yaml.safe_load(src.read_text(encoding='utf-8'))
    if species not in rules['species']:
        raise SystemExit(f'未知物种 {species}，可选：{", ".join(rules["species"])}')
    if road not in rules['roads']:
        raise SystemExit(f'未知路况 {road}，可选：{", ".join(rules["roads"])}')
    if load_ratio > 1.0 or load_ratio < 0:
        raise SystemExit('载重比须在 0..1 之间（>1 属于超载）')

    band = next(b for b in rules['load_bands'] if Fraction(str(load_ratio)) <= Fraction(str(b['max_share'])))
    base = Fraction(str(rules['base_miles_per_day']))
    hours = Fraction(str(rules['hours_per_day']))
    sp_f = Fraction(str(rules['species'][species]['factor']))
    ld_f = Fraction(str(band['factor']))
    rd_f = Fraction(str(rules['roads'][road]['factor']))

    miles_per_day = float(base * sp_f * ld_f * rd_f)
    mph = miles_per_day / float(hours)
    travel_hours = miles / mph if mph else 0.0
    travel_days = miles / miles_per_day if miles_per_day else 0.0

    return {
        'species': species,
        'load_band': band['name'],
        'road': road,
        'miles_per_day': round(miles_per_day, 2),
        'mph': round(mph, 3),
        'distance_miles': miles,
        'estimated_travel_hours': round(travel_hours, 2),
        'estimated_travel_days_8h': round(travel_days, 2),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description='地面旅行离线估算器')
    p.add_argument('--species', default='human')
    p.add_argument('--load-ratio', type=float, default=0.25)
    p.add_argument('--road', default='好')
    p.add_argument('--miles', type=float, default=30.0)
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    res = calculate(species=a.species, load_ratio=a.load_ratio, road=a.road, miles=a.miles)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"【旅行估时】{res['species']}·{res['load_band']}·{res['road']}路：{res['miles_per_day']} 英里/8h日（{res['mph']} mph）")
        print(f"  {res['distance_miles']} 英里 ≈ {res['estimated_travel_hours']} 小时行进（{res['estimated_travel_days_8h']} 个行进日）")


if __name__ == '__main__':
    main()
