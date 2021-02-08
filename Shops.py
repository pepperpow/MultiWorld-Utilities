from __future__ import annotations
from enum import unique, Enum
from typing import List, Union, Optional, Set, NamedTuple, Dict
import logging

from BaseClasses import Location
from EntranceShuffle import door_addresses
from Items import item_name_groups, item_table, ItemFactory, trap_replaceable
from Utils import int16_as_bytes

logger = logging.getLogger("Shops")


@unique
class ShopType(Enum):
    Shop = 0
    TakeAny = 1
    UpgradeShop = 2


class Shop():
    slots: int = 3  # slot count is not dynamic in asm, however inventory can have None as empty slots
    blacklist: Set[str] = set()  # items that don't work, todo: actually check against this
    type = ShopType.Shop

    def __init__(self, region, room_id: int, shopkeeper_config: int, custom: bool, locked: bool, sram_offset: int):
        self.region = region
        self.room_id = room_id
        self.inventory: List[Optional[dict]] = [None] * self.slots
        self.shopkeeper_config = shopkeeper_config
        self.custom = custom
        self.locked = locked
        self.sram_offset = sram_offset

    @property
    def item_count(self) -> int:
        for x in range(self.slots - 1, -1, -1):  # last x is 0
            if self.inventory[x]:
                return x + 1
        return 0

    def get_bytes(self) -> List[int]:
        # [id][roomID-low][roomID-high][doorID][zero][shop_config][shopkeeper_config][sram_index]
        entrances = self.region.entrances
        config = self.item_count
        if len(entrances) == 1 and entrances[0].name in door_addresses:
            door_id = door_addresses[entrances[0].name][0] + 1
        else:
            door_id = 0
            config |= 0x40  # ignore door id
        if self.type == ShopType.TakeAny:
            config |= 0x80
        elif self.type == ShopType.UpgradeShop:
            config |= 0x10  # Alt. VRAM
        return [0x00] + int16_as_bytes(self.room_id) + [door_id, 0x00, config, self.shopkeeper_config, 0x00]

    def has_unlimited(self, item: str) -> bool:
        for inv in self.inventory:
            if inv is None:
                continue
            if inv['max']:
                if inv['replacement'] == item:
                    return True
            elif inv['item'] == item:
                return True

        return False

    def has(self, item: str) -> bool:
        for inv in self.inventory:
            if inv is None:
                continue
            if inv['item'] == item:
                return True
            if inv['replacement'] == item:
                return True
        return False

    def clear_inventory(self):
        self.inventory = [None] * self.slots

    def add_inventory(self, slot: int, item: str, price: int, max: int = 0,
                      replacement: Optional[str] = None, replacement_price: int = 0, create_location: bool = False,
                      player: int = 0):
        self.inventory[slot] = {
            'item': item,
            'price': price,
            'max': max,
            'replacement': replacement,
            'replacement_price': replacement_price,
            'create_location': create_location,
            'player': player
        }

    def push_inventory(self, slot: int, item: str, price: int, max: int = 1, player: int = 0):
        if not self.inventory[slot]:
            raise ValueError("Inventory can't be pushed back if it doesn't exist")

        if not self.can_push_inventory(slot):
            logging.warning(f'Warning, there is already an item pushed into this slot.')

        self.inventory[slot] = {
            'item': item,
            'price': price,
            'max': max,
            'replacement': self.inventory[slot]["item"],
            'replacement_price': self.inventory[slot]["price"],
            'create_location': self.inventory[slot]["create_location"],
            'player': player
        }

    def can_push_inventory(self, slot: int):
        return self.inventory[slot] and not self.inventory[slot]["replacement"]


class TakeAny(Shop):
    type = ShopType.TakeAny


class UpgradeShop(Shop):
    type = ShopType.UpgradeShop
    # Potions break due to VRAM flags set in UpgradeShop.
    # Didn't check for more things breaking as not much else can be shuffled here currently
    blacklist = item_name_groups["Potions"]


shop_class_mapping = {ShopType.UpgradeShop: UpgradeShop,
                      ShopType.Shop: Shop,
                      ShopType.TakeAny: TakeAny}


def FillDisabledShopSlots(world):
    shop_slots: Set[Location] = {location for shop_locations in (shop.region.locations for shop in world.shops)
                                 for location in shop_locations if location.shop_slot and location.shop_slot_disabled}
    for location in shop_slots:
        location.shop_slot_disabled = True
        slot_num = int(location.name[-1]) - 1
        shop: Shop = location.parent_region.shop
        location.item = ItemFactory(shop.inventory[slot_num]['item'], location.player)
        location.item_rule = lambda item: item.name == location.item.name and item.player == location.player


def ShopSlotFill(world):
    shop_slots: Set[Location] = {location for shop_locations in (shop.region.locations for shop in world.shops)
                                 for location in shop_locations if location.shop_slot}
    removed = set()
    for location in shop_slots:
        slot_num = int(location.name[-1]) - 1
        shop: Shop = location.parent_region.shop
        if not shop.can_push_inventory(slot_num) or location.shop_slot_disabled:
            location.shop_slot_disabled = True
            removed.add(location)

    if removed:
        shop_slots -= removed

    if shop_slots:
        from Fill import swap_location_item
        # TODO: allow each game to register a blacklist to be used here?
        blacklist_words = {"Rupee"}
        blacklist_words = {item_name for item_name in item_table if any(
            blacklist_word in item_name for blacklist_word in blacklist_words)}
        blacklist_words.add("Bee")
        candidates_per_sphere = list(list(sphere) for sphere in world.get_spheres())

        candidate_condition = lambda location: not location.locked and \
                                               not location.shop_slot and \
                                               not location.item.name in blacklist_words

        # currently special care needs to be taken so that Shop.region.locations.item is identical to Shop.inventory
        # Potentially create Locations as needed and make inventory the only source, to prevent divergence
        cumu_weights = []

        for sphere in candidates_per_sphere:
            if cumu_weights:
                x = cumu_weights[-1]
            else:
                x = 0
            cumu_weights.append(len(sphere) + x)
            world.random.shuffle(sphere)

        for i, sphere in enumerate(candidates_per_sphere):
            current_shop_slots = [location for location in sphere if location.shop_slot and not location.shop_slot_disabled]
            if current_shop_slots:

                for location in current_shop_slots:
                    shop: Shop = location.parent_region.shop
                    # TODO: might need to implement trying randomly across spheres until canditates are exhausted.
                    # As spheres may be as small as one item.
                    swapping_sphere = world.random.choices(candidates_per_sphere[i:], cum_weights=cumu_weights[i:])[0]
                    for c in swapping_sphere:  # chosen item locations
                        if candidate_condition(c) and c.item_rule(location.item) and location.item_rule(c.item):
                            swap_location_item(c, location, check_locked=False)
                            logger.debug(f'Swapping {c} into {location}:: {location.item}')
                            break

                    else:
                        # This *should* never happen. But let's fail safely just in case.
                        logger.warning("Ran out of ShopShuffle Item candidate locations.")
                        location.shop_slot_disabled = True
                        continue
                    item_name = location.item.name
                    if any(x in item_name for x in ['Single Bomb', 'Single Arrow', 'Piece of Heart']):
                        price = world.random.randrange(1, 7)
                    elif any(x in item_name for x in ['Arrow', 'Bomb', 'Clock']):
                        price = world.random.randrange(2, 14)
                    elif any(x in item_name for x in ['Compass', 'Map', 'Small Key', 'Clock', 'Heart']):
                        price = world.random.randrange(4, 28)
                    else:
                        price = world.random.randrange(8, 56)

                    price *=5

                    if 'p' in world.shop_shuffle[location.player]:
                        price = return_funny_price(price, item_name, world, location.player)
                    
                    shop.push_inventory(int(location.name[-1]) - 1, item_name, price, 1,
                                        location.item.player if location.item.player != location.player else 0)

price_blacklist = {'rupee': {'Rupees'}, 'heart': {'Small Heart', 'Apple'}, 'magic': {'Magic Jar'}, 'bomb': {'Bombs', 'Single Bomb'},
                        'arrow': {'Arrows', 'Single Arrow'}, 'ch': {}, 'cb': {}, 'ca': {}, 'keys': {}, 'potion': {}}

def return_funny_price(price, item_name, world, player):
    if item_name is None:
        return price
    price = max(1, price//5) # funny units
    my_price_types = [x for x in price_blacklist]
    my_choices = world.random.sample(my_price_types, len(my_price_types))
    for p in my_choices:
        if any(x in item_name for x in price_blacklist[p]):
            continue
        if p in ['rupee', 'cb', 'ca'] or (p == 'keys' and world.keyshuffle[player] != 'universal'):
            price *= 5
        else:
            if p == 'heart': price = min(0x10, price)*4
            if p == 'magic': price = min(0x20, price)*4
            if p == 'bomb': price = min(10, price//4)
            if p == 'arrow': price = min(30, price//2)
            if p == 'ch': price = min(0x3, max(0x1, price//8))*8
            if p == 'keys': price = min(0x3, max(0x1, price//8))
            if p == 'potion': price = world.random.randrange(0, 6)
            price |= 0x8000 | (0x100*(my_price_types.index(p)-1))
        break
    return price

def create_shops(world, player: int):
    option = world.shop_shuffle[player]

    player_shop_table = shop_table.copy()
    if "w" in option:
        player_shop_table["Potion Shop"] = player_shop_table["Potion Shop"]._replace(locked=False)
        dynamic_shop_slots = total_dynamic_shop_slots + 3
    else:
        dynamic_shop_slots = total_dynamic_shop_slots

    num_slots = min(dynamic_shop_slots, max(0, int(world.shop_shuffle_slots[player])))  # 0 to 30
    single_purchase_slots: List[bool] = [True] * num_slots + [False] * (dynamic_shop_slots - num_slots)
    world.random.shuffle(single_purchase_slots)

    if 'g' in option or 'f' in option:
        default_shop_table = [i for l in [shop_generation_types[x] for x in ['arrows', 'bombs', 'potions', 'shields', 'bottle'] if not world.retro[player] or x != 'arrows'] for i in l]
        new_basic_shop = world.random.sample(default_shop_table, k=3)
        new_dark_shop = world.random.sample(default_shop_table, k=3)
        for name, shop in player_shop_table.items():
            typ, shop_id, keeper, custom, locked, items, sram_offset = shop
            if not locked:
                new_items = world.random.sample(default_shop_table, k=3)
                if 'f' not in option:
                    if items == _basic_shop_defaults:
                        new_items = new_basic_shop
                    elif items == _dark_world_shop_defaults:
                        new_items = new_dark_shop
                keeper = world.random.choice([0xA0, 0xC1, 0xFF])
                player_shop_table[name] = ShopData(typ, shop_id, keeper, custom, locked, new_items, sram_offset)
    if world.mode[player] == "inverted":
        player_shop_table["Dark Lake Hylia Shop"] = \
            player_shop_table["Dark Lake Hylia Shop"]._replace(locked=True, items=_inverted_hylia_shop_defaults)
    for region_name, (room_id, type, shopkeeper, custom, locked, inventory, sram_offset) in player_shop_table.items():
        region = world.get_region(region_name, player)
        shop: Shop = shop_class_mapping[type](region, room_id, shopkeeper, custom, locked, sram_offset)
        region.shop = shop
        world.shops.append(shop)
        for index, item in enumerate(inventory):
            shop.add_inventory(index, *item)
            if not locked and num_slots:
                slot_name = "{} Slot {}".format(region.name, index + 1)
                loc = Location(player, slot_name, address=shop_table_by_location[slot_name],
                               parent=region, hint_text="for sale")
                loc.shop_slot = True
                loc.locked = True
                if single_purchase_slots.pop():
                    if world.goal[player] != 'icerodhunt':
                        additional_item = 'Rupees (50)'  # world.random.choice(['Rupees (50)', 'Rupees (100)', 'Rupees (300)'])
                    else:
                        additional_item = 'Nothing'
                    loc.item = ItemFactory(additional_item, player)
                else:
                    loc.item = ItemFactory('Nothing', player)
                    loc.shop_slot_disabled = True
                shop.region.locations.append(loc)
                world.dynamic_locations.append(loc)
                world.clear_location_cache()


class ShopData(NamedTuple):
    room: int
    type: ShopType
    shopkeeper: int
    custom: bool
    locked: bool
    items: List
    sram_offset: int


# (type, room_id, shopkeeper, custom, locked, [items], sram_offset)
# item = (item, price, max=0, replacement=None, replacement_price=0)
_basic_shop_defaults = [('Red Potion', 150), ('Small Heart', 10), ('Bombs (10)', 50)]
_dark_world_shop_defaults = [('Red Potion', 150), ('Blue Shield', 50), ('Bombs (10)', 50)]
_inverted_hylia_shop_defaults = [('Blue Potion', 160), ('Blue Shield', 50), ('Bombs (10)', 50)]
shop_table: Dict[str, ShopData] = {
    'Cave Shop (Dark Death Mountain)': ShopData(0x0112, ShopType.Shop, 0xC1, True, False, _basic_shop_defaults, 0),
    'Red Shield Shop': ShopData(0x0110, ShopType.Shop, 0xC1, True, False,
                                [('Red Shield', 500), ('Bee', 10), ('Arrows (10)', 30)], 3),
    'Dark Lake Hylia Shop': ShopData(0x010F, ShopType.Shop, 0xC1, True, False, _dark_world_shop_defaults, 6),
    'Dark World Lumberjack Shop': ShopData(0x010F, ShopType.Shop, 0xC1, True, False, _dark_world_shop_defaults, 9),
    'Village of Outcasts Shop': ShopData(0x010F, ShopType.Shop, 0xC1, True, False, _dark_world_shop_defaults, 12),
    'Dark World Potion Shop': ShopData(0x010F, ShopType.Shop, 0xC1, True, False, _dark_world_shop_defaults, 15),
    'Light World Death Mountain Shop': ShopData(0x00FF, ShopType.Shop, 0xA0, True, False, _basic_shop_defaults, 18),
    'Kakariko Shop': ShopData(0x011F, ShopType.Shop, 0xA0, True, False, _basic_shop_defaults, 21),
    'Cave Shop (Lake Hylia)': ShopData(0x0112, ShopType.Shop, 0xA0, True, False, _basic_shop_defaults, 24),
    'Potion Shop': ShopData(0x0109, ShopType.Shop, 0xA0, True, True,
                            [('Red Potion', 120), ('Green Potion', 60), ('Blue Potion', 160)], 27),
    'Capacity Upgrade': ShopData(0x0115, ShopType.UpgradeShop, 0x04, True, True,
                                 [('Bomb Upgrade (+5)', 100, 7), ('Arrow Upgrade (+5)', 100, 7)], 30)
}

total_shop_slots = len(shop_table) * 3
total_dynamic_shop_slots = sum(3 for shopname, data in shop_table.items() if not data[4])  # data[4] -> locked

SHOP_ID_START = 0x400000
shop_table_by_location_id = {cnt: s for cnt, s in enumerate(
    (f"{name} Slot {num}" for name in [key for key, value in sorted(shop_table.items(), key=lambda item: item[1].sram_offset)]
     for num in range(1, 4)), start=SHOP_ID_START)}
shop_table_by_location_id[(SHOP_ID_START + total_shop_slots)] = "Old Man Sword Cave"
shop_table_by_location_id[(SHOP_ID_START + total_shop_slots + 1)] = "Take-Any #1"
shop_table_by_location_id[(SHOP_ID_START + total_shop_slots + 2)] = "Take-Any #2"
shop_table_by_location_id[(SHOP_ID_START + total_shop_slots + 3)] = "Take-Any #3"
shop_table_by_location_id[(SHOP_ID_START + total_shop_slots + 4)] = "Take-Any #4"
shop_table_by_location = {y: x for x, y in shop_table_by_location_id.items()}

shop_generation_types = {
    'arrows': [('Arrows (10)', 50), ('Arrows (10)', 50)],
    'bombs': [('Single Bomb', 10), ('Bombs (3)', 30), ('Bombs (10)', 50)],
    'shields': [('Red Shield', 500), ('Blue Shield', 50)],
    'potions': [('Red Potion', 150), ('Green Potion', 90), ('Blue Potion', 190)],
    'discount_potions': [('Red Potion', 120), ('Green Potion', 60), ('Blue Potion', 160)],
    'bottle': [('Small Heart', 10), ('Apple', 50), ('Bee', 10), ('Good Bee', 100), ('Faerie', 100), ('Magic Jar', 100)],
    'time': [('Red Clock', 100), ('Blue Clock', 200), ('Green Clock', 300)],
}


def set_up_shops(world, player: int):
    # TODO: move hard+ mode changes for shields here, utilizing the new shops

    if world.retro[player]:
        rss = world.get_region('Red Shield Shop', player).shop
        replacement_items = [['Red Potion', 150], ['Green Potion', 75], ['Blue Potion', 200], ['Bombs (10)', 50],
                             ['Blue Shield', 50], ['Small Heart', 10]]  # Can't just replace the single arrow with 10 arrows as retro doesn't need them.
        if world.keyshuffle[player] == "universal":
            replacement_items.append(['Small Key (Universal)', 100])
        replacement_item = world.random.choice(replacement_items)
        rss.add_inventory(2, 'Single Arrow', 80, 1, replacement_item[0], replacement_item[1])
        rss.locked = True

    if world.keyshuffle[player] == "universal" or world.retro[player]:
        for shop in world.random.sample([s for s in world.shops if
                                         s.custom and not s.locked and s.type == ShopType.Shop and s.region.player == player],
                                        5):
            shop.locked = True
            slots = [0, 1, 2]
            world.random.shuffle(slots)
            slots = iter(slots)
            if world.keyshuffle[player] == "universal":
                shop.add_inventory(next(slots), 'Small Key (Universal)', 100)
            if world.retro[player]:
                shop.push_inventory(next(slots), 'Single Arrow', 80)


def shuffle_shops(world, items, player: int):
    option = world.shop_shuffle[player]
    if 'u' in option:
        progressive = world.progressive[player]
        progressive = world.random.choice([True, False]) if progressive == 'random' else progressive == 'on'
        progressive &= world.goal == 'icerodhunt'
        new_items = ["Bomb Upgrade (+5)"] * 6
        new_items.append("Bomb Upgrade (+5)" if progressive else "Bomb Upgrade (+10)")

        if not world.retro[player]:
            new_items += ["Arrow Upgrade (+5)"] * 6
            new_items.append("Arrow Upgrade (+5)" if progressive else "Arrow Upgrade (+10)")

        world.random.shuffle(new_items)  # Decide what gets tossed randomly if it can't insert everything.

        capacityshop: Optional[Shop] = None
        for shop in world.shops:
            if shop.type == ShopType.UpgradeShop and shop.region.player == player and \
                    shop.region.name == "Capacity Upgrade":
                shop.clear_inventory()
                capacityshop = shop

        if world.goal[player] != 'icerodhunt':
            for i, item in enumerate(items):
                if item.name in trap_replaceable:
                    items[i] = ItemFactory(new_items.pop(), player)
                    if not new_items:
                        break
            else:
                logging.warning(f"Not all upgrades put into Player{player}' item pool. Putting remaining items in Capacity Upgrade shop instead.")
                bombupgrades = sum(1 for item in new_items if 'Bomb Upgrade' in item)
                arrowupgrades = sum(1 for item in new_items if 'Arrow Upgrade' in item)
                if bombupgrades:
                    capacityshop.add_inventory(1, 'Bomb Upgrade (+5)', 100, bombupgrades)
                if arrowupgrades:
                    capacityshop.add_inventory(1, 'Arrow Upgrade (+5)', 100, arrowupgrades)
        else:
            for item in new_items:
                world.push_precollected(ItemFactory(item, player))

    if 'p' in option or 'i' in option:
        shops = []
        upgrade_shops = []
        total_inventory = []
        for shop in world.shops:
            if shop.region.player == player:
                if shop.type == ShopType.UpgradeShop:
                    upgrade_shops.append(shop)
                elif shop.type == ShopType.Shop:
                    if shop.region.name == 'Potion Shop' and not 'w' in option:
                        # don't modify potion shop
                        pass
                    else:
                        shops.append(shop)
                        total_inventory.extend(shop.inventory)

        if 'p' in option:
            def price_adjust(price: int) -> int:
                # it is important that a base price of 0 always returns 0 as new price!
                adjust = 2 if price < 100 else 5
                return int((price / adjust) * (0.5 + world.random.random() * 1.5)) * adjust

            def adjust_item(item):
                if item:
                    item["price"] = return_funny_price(price_adjust(item["price"]), item['item'], world, player)
                    item['replacement_price'] = return_funny_price(price_adjust(item["replacement_price"]), item['replacement'], world, player)

            for item in total_inventory:
                adjust_item(item)
            for shop in upgrade_shops:
                for item in shop.inventory:
                    adjust_item(item)

        if 'i' in option:
            world.random.shuffle(total_inventory)

            i = 0
            for shop in shops:
                slots = shop.slots
                shop.inventory = total_inventory[i:i + slots]
                i += slots
