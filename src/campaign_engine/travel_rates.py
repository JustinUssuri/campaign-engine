"""地面旅行的只读计算：完整负荷、物种与路况一次相乘。

每个移动单元分别计算，再取队伍瓶颈。NPC商队是整队模板，不扫描其货物。
未知重量/数量、超载和不支持的形态不产生可执行速度；不写入state。
"""
import copy
import math
from fractions import Fraction

import yaml

from . import flight as F, haul, items as I
from .config import is_pov, pov_id
from .store import ROOT, cached, pkg_data, read_text

PATH = ROOT / 'rules' / 'travel.yaml'
KM_PER_MILE = 1.609344


def settings():
    def build():
        src = PATH if PATH.exists() else pkg_data('rules', 'travel.yaml')
        data = yaml.safe_load(read_text(src)) or {}
        if data.get('version') != 1:
            raise SystemExit('旅行倍率配置版本不支持')
        _positive(data.get('base_miles_per_day'), '基准日程')
        _positive(data.get('hours_per_day'), '行进日小时数')
        for row in data.get('species', {}).values():
            _positive(row.get('factor'), '物种倍率')
        for row in data.get('roads', {}).values():
            _positive(row.get('factor'), '路况倍率')
        for row in data.get('load_bands', []):
            _positive(row.get('max_share'), '载重阈值')
            _positive(row.get('factor'), '载重倍率')
        return data
    src = PATH if PATH.exists() else pkg_data('rules', 'travel.yaml')
    return copy.deepcopy(cached(('travel-rates',), [src], build))


def _fraction(value):
    if isinstance(value, bool) or value is None:
        raise ValueError
    result = Fraction(str(value))
    if not math.isfinite(float(result)):
        raise ValueError
    return result


def _positive(value, name):
    try:
        result = _fraction(value)
        if result <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise SystemExit(f'旅行{name}须为有效正数') from exc
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def road_factor(road, rules=None):
    rules = settings() if rules is None else rules
    row = rules.get('roads', {}).get(road)
    if row is None:
        raise SystemExit('旅行路况写 好、差、很差；无法通行须另定路线')
    return float(_positive(row['factor'], '路况倍率'))


def speed(species, load_lb, capacity_lb, road='好', rules=None):
    """单元纯计算；阈值用分数比较，等于⅓/⅔不误升档。"""
    rules = settings() if rules is None else rules
    rfactor = road_factor(road, rules)
    result = {'species': species, 'species_factor': None, 'known_lb': load_lb,
              'capacity_lb': capacity_lb, 'load_ratio': None, 'load_band': None,
              'load_factor': None, 'road': road, 'road_factor': rfactor,
              'mph': None, 'miles_per_day': None, 'km_per_day': None,
              'unknown': [], 'issues': []}
    row = rules.get('species', {}).get(species)
    if row is None:
        result['unknown'].append('当前物种/形态不在地面旅行倍率表，须先核定专用规则')
    else:
        result['species_factor'] = float(_positive(row['factor'], '物种倍率'))
    load = _number(load_lb)
    cap = _number(capacity_lb)
    if load is None:
        result['unknown'].append('负荷重量未知或无效')
    if cap is None or cap == 0:
        result['unknown'].append('承载上限未知或无效')
    if result['unknown']:
        result['issues'] = list(result['unknown'])
        return result
    load_share = _fraction(load_lb) / _fraction(capacity_lb)
    result['load_ratio'] = float(load_share)
    if load_share > 1:
        result['issues'].append(f'超载 {float(_fraction(load_lb)-_fraction(capacity_lb)):g} 磅；先减载或增加运力')
        return result
    band = next((b for b in rules['load_bands'] if load_share <= _fraction(b['max_share'])), None)
    if band is None:
        raise SystemExit('旅行载重倍率表未覆盖合规负荷')
    result['load_band'] = band['name']
    result['load_factor'] = float(_positive(band['factor'], '载重倍率'))
    assert row is not None
    day = (_positive(rules['base_miles_per_day'], '基准日程')
           * _positive(row['factor'], '物种倍率')
           * _positive(band['factor'], '载重倍率')
           * _positive(rules['roads'][road]['factor'], '路况倍率'))
    result['miles_per_day'] = float(day)
    result['km_per_day'] = float(day) * KM_PER_MILE
    result['mph'] = float(day / _positive(rules['hours_per_day'], '行进日小时数'))
    return result


def _actor_species(state, who, rules):
    items = state.get('items') or {}
    item = items.get(who)
    if item is not None:
        typ = item.get('type')
        if typ:
            typ = I.canonical(typ)
            return rules.get('animal_types', {}).get(typ)
        # 老Draft Horse卡曾被骡共用，缺type时不能猜是哪种动物。
        return rules.get('legacy_cards', {}).get(I.card_of(item))
    char = (state.get('characters') or {}).get(who) or {}
    form = char.get('current_form')
    if is_pov(who, state):
        return 'human' if form in ('elf', 'human', '精灵', '人类') else None
    card, _ = F._card(state, who)
    if form is not None and form not in ('elf', 'human', '精灵', '人类'):
        return None
    if card.get('卡名'):
        return rules.get('legacy_cards', {}).get(card['卡名'])
    # 同伴人物卡是步行类人的既有接口；缺卡/缺能力仍会被capacity核验阻止。
    return 'human' if card and card.get('体型') in ('微型', '小型', '中型', 'Tiny', 'Small', 'Medium') else None


def _capacity(state, who):
    item = (state.get('items') or {}).get(who)
    if item is not None:
        card = I.card_of(item)
        c = haul.carry(card) if card else None
        return c[0] if c else None
    return I.person_capacity(who, state)


class _Mass:
    """只走所选运输单元的容纳链；不核对无关NPC的库存。"""
    def __init__(self, state):
        self.state = state
        self.items = state.get('items') or {}
        self.cat = I.catalog()
        self.children = {}
        for iid, item in self.items.items():
            self.children.setdefault(item.get('in'), []).append(iid)
        self.unknown = []
        self.details = []
        self.total = Fraction(0)
        self.seen = set()
        self.visiting = set()
        self.cash_seen = set()

    def add(self, value, name, kind):
        value = _fraction(value)
        if not math.isfinite(float(self.total + value)):
            self.unknown.append(f'{name}重量合计无效')
            return
        self.total += value
        self.details.append({'name': name, 'kind': kind, 'lb': float(value)})

    def cash(self, who, charge=True):
        if who in self.cash_seen:
            return
        self.cash_seen.add(who)
        if not charge:
            return
        value = F._mass(self.state, who, 'cash')
        if value is None:
            self.unknown.append(f'{F.label(self.state, who)}钱袋实重未知或余额已变化；先 weigh --cash-lb 核定（明确无现金也写0）')
        else:
            self.add(value, F.label(self.state, who) + '钱袋', 'cash')

    def item(self, iid, charge=True, body=True, skip_animals=None):
        if iid in self.visiting:
            self.unknown.append(f'物品容纳关系循环：{iid}')
            return
        if iid in self.seen:
            return
        self.visiting.add(iid)
        item = self.items[iid]
        typedef = I.tdef(item, self.cat)
        kind = typedef.get('kind')
        name = I.label(item, self.cat)
        qty = _number(item.get('qty', 1))
        if qty is None:
            self.unknown.append(f'{name}数量未知或无效')
        if charge and body:
            own = _number(typedef.get('lb'))
            if kind == 'animal' and typedef.get('lb') is None:
                own = F._mass(self.state, iid, 'body')
            if own is None and typedef.get('lb') is None and I.unit_of(item, self.cat) == '磅' and qty is not None:
                own, qty = qty, 1.0
            if own is None:
                self.unknown.append(f'{name}自重未知或无效；核定货品类型重量')
            elif qty is not None:
                self.add(_fraction(own) * _fraction(qty), name, 'body' if kind == 'animal' else 'item')
        content_charge = charge and not typedef.get('weightless')
        for child in self.children.get(iid, []):
            if child in (skip_animals or ()):
                continue
            self.item(child, content_charge, skip_animals=skip_animals)
        if kind == 'animal' and F.cash_snapshot(self.state, iid):
            self.cash(iid, charge)
        self.visiting.remove(iid)
        self.seen.add(iid)

    def actor(self, who, body=False):
        if who in self.items:
            self.item(who, body=body)
            return
        if body:
            value = F._mass(self.state, who, 'body')
            if value is None:
                self.unknown.append(f'{F.label(self.state, who)}当前形态裸重未知；先 weigh --body-lb 核定')
            else:
                self.add(value, F.label(self.state, who) + '裸重', 'body')
        for iid in self.children.get(who, []):
            if I.is_team(self.items[iid], self.cat):
                continue
            self.item(iid)
        self.cash(who)


def _unit(state, unit, road, rules):
    kind = unit.get('kind')
    who = unit.get('who')
    riders = list(unit.get('riders') or [])
    unknown, issues = [], []
    mass = _Mass(state)
    capacity, species = None, None
    team = []
    if kind == 'walk':
        if who in (state.get('items') or {}):
            unknown.append('动物须用ride单元，驮畜的riders为空')
        if riders:
            unknown.append('步行单元不能带骑手列表')
        species = _actor_species(state, who, rules)
        capacity = _capacity(state, who)
        mass.actor(who)
    elif kind == 'ride':
        animal = (state.get('items') or {}).get(who)
        if animal is None or I.kind_of(animal) != 'animal':
            unknown.append('骑乘/驮运单元须指向已登记牲口')
        else:
            parent = (state.get('items') or {}).get(animal.get('in'))
            if animal.get('how') == '套着' or (parent and I.kind_of(parent) == 'vehicle'):
                unknown.append(f'{F.label(state, who)}仍套车，不能同时登记为独立驮畜')
            species = _actor_species(state, who, rules)
            capacity = _capacity(state, who)
            mass.actor(who)
        for rider in riders:
            mount_card, _ = F._card(state, who)
            rider_card, _ = F._card(state, rider)
            mount_size = str(mount_card.get('体型') or '')
            rider_size = str(rider_card.get('体型') or '')
            mount_size = F.SIZE_EN.get(mount_size, mount_size)
            rider_size = F.SIZE_EN.get(rider_size, rider_size)
            if mount_size not in haul.SIZES or rider_size not in haul.SIZES:
                unknown.append(f'{F.label(state, rider)}或坐骑体型未知，不能确认骑乘')
            elif haul.SIZES.index(mount_size) <= haul.SIZES.index(rider_size):
                unknown.append(f'{F.label(state, who)}体型须比骑手{F.label(state, rider)}至少大一级')
            mass.actor(rider, body=True)
    elif kind == 'cart':
        vehicle = (state.get('items') or {}).get(who)
        if vehicle is None or I.kind_of(vehicle) != 'vehicle':
            unknown.append('车辆单元须指向已登记车辆')
        else:
            team = [aid for aid in I.hitched(state, who) if I.how_of(state, aid) == '套着']
            mass.item(who, skip_animals=set(team))
            if not team:
                unknown.append(f'{F.label(state, who)}没有已套牲口')
            capacities, species_rows = [], []
            for aid in team:
                animal = state['items'][aid]
                card = I.card_of(animal)
                c = haul.carry(card) if card else None
                if c is None:
                    unknown.append(f'{F.label(state, aid)}牵引上限未知')
                else:
                    capacities.append(c[1])
                sp = _actor_species(state, aid, rules)
                if sp not in rules['species']:
                    unknown.append(f'{F.label(state, aid)}物种倍率未登记')
                else:
                    species_rows.append(sp)
                # 牵引畜的自身负载不能被忽略；此种双用途需明确拆分安排。
                for child in mass.children.get(aid, []):
                    typ = state['items'][child].get('type')
                    if typ and I.canonical(typ) == 'harness':
                        mass.item(child)
                    else:
                        unknown.append(f'{F.label(state, aid)}套车同时另驮物，须先核定两种劳动的分配')
            capacity = sum(capacities) if len(capacities) == len(team) and capacities else None
            if len(species_rows) == len(team) and species_rows:
                species = min(species_rows, key=lambda sp: float(_positive(rules['species'][sp]['factor'], '物种倍率')))
        for rider in riders:
            mass.actor(rider, body=True)
    else:
        unknown.append('运输单元kind写walk、ride或cart')
    result = speed(species, float(mass.total), capacity, road, rules)
    result.update(kind=kind, who=who, name=F.label(state, who), riders=riders,
                  team=team, details=mass.details)
    result['unknown'] = list(dict.fromkeys(unknown + mass.unknown + result['unknown']))
    result['issues'] = list(dict.fromkeys(issues + result['unknown'] + result['issues']))
    if result['issues']:
        result.update(mph=None, miles_per_day=None, km_per_day=None)
    return result


def _caravan(caravan, road, rules):
    if caravan is None:
        return None
    if isinstance(caravan, str):
        row = rules.get('caravans', {}).get(caravan)
        if row is None:
            raise SystemExit('商队模板写 horse、camel、mule、donkey，或已核定标准速度')
        mph = float(_positive(row['miles_per_day'], '商队日程') / _positive(rules['hours_per_day'], '行进日小时数'))
        return {'id': caravan, 'name': row['name'], 'mph': mph * road_factor(road, rules),
                'source': 'rules/travel.yaml：正常商业满载整队模板', 'road_included': True,
                'transport_kind': row['transport_kind']}
    if not isinstance(caravan, dict) or not isinstance(caravan.get('source'), str) or not caravan['source'].strip():
        raise SystemExit('商队自定标准速度须有非空source，不从含停歇的总天数倒推')
    mph = float(_positive(caravan.get('mph'), '商队小时进度'))
    if not caravan.get('road_included', True):
        mph *= road_factor(road, rules)
    transport_kind = caravan.get('transport_kind')
    if transport_kind not in (None, 'cart', 'pack', 'ride', 'walk'):
        raise SystemExit('商队transport_kind写cart、pack、ride或walk')
    return {'id': caravan.get('id'), 'name': caravan.get('name', '已定商队'), 'mph': mph,
            'source': caravan['source'], 'road_included': True, 'transport_kind': transport_kind}


def report(state, units=None, road='好', caravan=None, rules=None):
    """只核传入的自有运输单元；返回速度、缺项、跟队能力，不盘NPC商队库存。"""
    rules = settings() if rules is None else copy.deepcopy(rules)
    road_factor(road, rules)
    units = [{'kind': 'walk', 'who': pov_id(state)}] if units is None else copy.deepcopy(units)
    if not isinstance(units, list) or not units:
        raise SystemExit('至少登记一个自己的运输单元')
    unknown, issues, normalized = [], [], []
    actors, hitch = set(), set()
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get('who'), str) or not unit['who']:
            raise SystemExit('运输单元须有kind与who')
        riders = unit.get('riders', [])
        if not isinstance(riders, list) or any(not isinstance(r, str) or not r for r in riders):
            raise SystemExit('乘员riders须为人物ID列表')
        if len(set(riders)) != len(riders):
            issues.append('同一运输单元重复登记乘员')
        members = [unit['who'], *riders]
        if len(set(members)) != len(members) or actors.intersection(members):
            issues.append('人物或运输单元重复登记，不能同时步行与乘车/骑乘')
        actors.update(members)
        row = _unit(state, unit, road, rules)
        if hitch.intersection(row['team']):
            issues.append('同一牲口不能牵引两辆车')
        hitch.update(row['team'])
        normalized.append(row)
        unknown.extend(row['unknown'])
        issues.extend(row['issues'])
    if actors.intersection(hitch):
        issues.append('套车牲口不能再作为独立运输单元')
    caravan_row = _caravan(caravan, road, rules)
    capability = None if issues else min(row['mph'] for row in normalized)
    can_follow = None if caravan_row is None or capability is None else capability >= caravan_row['mph'] - 1e-12
    mph = capability
    if caravan_row is not None and capability is not None:
        mph = min(capability, caravan_row['mph'])
        if not can_follow:
            issues.append('自己的运输单元跟不上已定商队；先减载、增运力或另作安排，不自动拖慢NPC商队')
    day = None if mph is None else mph * float(_positive(rules['hours_per_day'], '行进日小时数'))
    return {'units': normalized, 'road': road, 'capability_mph': capability,
            'mph': mph, 'miles_per_day': day, 'km_per_day': None if day is None else day * KM_PER_MILE,
            'unknown': list(dict.fromkeys(unknown)), 'issues': list(dict.fromkeys(issues)),
            'blocked': bool(issues), 'caravan': caravan_row, 'can_follow': can_follow,
            'rules_version': rules['version']}


def format_report(result):
    out = []
    for row in result['units']:
        cap = '?' if row['capacity_lb'] is None else f"{row['capacity_lb']:g}"
        speed_text = '待核' if row['mph'] is None else f"{row['mph']:g}英里/小时，{row['km_per_day']:.1f}公里/行进日"
        factors = '' if row['load_band'] is None else f"｜{row['load_band']}｜物种×{row['species_factor']:g} 载重×{row['load_factor']:g} 路况×{row['road_factor']:g}"
        out.append(f"{row['name']}〔{row['kind']}〕负荷 {row['known_lb']:g}/{cap}磅{factors}｜{speed_text}")
    if result['caravan']:
        caravan = result['caravan']
        out.append(f"商队整队模板：{caravan['name']}｜{caravan['mph']:g}英里/小时（不逐车核货）")
    if result['mph'] is not None:
        out.append(f"整队进度：{result['mph']:g}英里/小时，{result['km_per_day']:.1f}公里/八小时行进日")
    for issue in result['issues']:
        out.append('待处理：' + issue)
    return '\n'.join(out)
