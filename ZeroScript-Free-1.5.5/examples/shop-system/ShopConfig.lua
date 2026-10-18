--!strict
-- ══════════════════════════════════════════════════════════════════════════
--  ShopConfig  ·  ModuleScript  ·  game.ReplicatedStorage.ShopConfig
-- ══════════════════════════════════════════════════════════════════════════
--  Pure data and shared types. No services, no side effects, nothing that can
--  yield: both sides require this, and it must load identically on the client
--  and the server.
--
--  SECURITY: the client may READ prices from here to draw a shop, but the
--  server re-reads the same table and never accepts a price, quantity or item
--  id from the client as truth. Editing this file on the client (an exploit
--  can) changes nothing about what a purchase costs.

export type Rarity = "common" | "uncommon" | "rare" | "legendary"

export type ItemData = {
	id: string,             -- stable key; saved in the DataStore, so never rename
	name: string,
	description: string,
	price: number,          -- server-authoritative cost, in Cash
	tab: string,            -- must match a TabData.id
	icon: string,           -- "rbxassetid://..." or "" for the initials fallback
	rarity: Rarity,
	maxOwned: number,       -- 0 = unlimited
}

export type TabData = {
	id: string,
	label: string,
	order: number,          -- tabs are sorted by this, so the array order is free
}

export type Palette = {
	background: Color3,
	card: Color3,
	border: Color3,
	accent: Color3,
	accentDim: Color3,
	textPrimary: Color3,
	textSecondary: Color3,
	danger: Color3,
	rarity: { [Rarity]: Color3 },
}

export type SoundSet = {
	hover: string,          -- "" = silent; fill in rbxassetid:// ids you own
	click: string,
	success: string,
	denied: string,
}

export type ShopData = {
	title: string,
	currencyName: string,
	startingCash: number,
	maxPurchaseQuantity: number,
	purchaseCooldown: number,     -- seconds between two purchases by one player
	rateLimitWindow: number,      -- rolling window, seconds
	rateLimitMax: number,         -- max purchase attempts inside that window
	toggleKey: Enum.KeyCode,
	openOnSpawn: boolean,
	tabs: { TabData },
	items: { ItemData },
	palette: Palette,
	sounds: SoundSet,
}

-- ── Dark-mode palette (one place, so client and any future HUD agree) ────────
local PALETTE: Palette = {
	background = Color3.fromRGB(20, 22, 28),
	card = Color3.fromRGB(28, 31, 39),
	border = Color3.fromRGB(45, 50, 62),
	accent = Color3.fromRGB(0, 190, 125),
	accentDim = Color3.fromRGB(0, 140, 255),
	textPrimary = Color3.fromRGB(245, 245, 250),
	textSecondary = Color3.fromRGB(160, 165, 180),
	danger = Color3.fromRGB(235, 87, 87),
	rarity = {
		common = Color3.fromRGB(160, 165, 180),
		uncommon = Color3.fromRGB(0, 190, 125),
		rare = Color3.fromRGB(0, 140, 255),
		legendary = Color3.fromRGB(255, 176, 32),
	},
}

local TABS: { TabData } = {
	{ id = "gear", label = "Gear", order = 1 },
	{ id = "boosts", label = "Boosts", order = 2 },
	{ id = "cosmetics", label = "Cosmetics", order = 3 },
}

local ITEMS: { ItemData } = {
	{
		id = "iron_sword", name = "Iron Sword", price = 250, tab = "gear",
		description = "A dependable blade. +8 melee damage while equipped.",
		icon = "", rarity = "common", maxOwned = 1,
	},
	{
		id = "grapple_hook", name = "Grapple Hook", price = 900, tab = "gear",
		description = "Pull yourself to any surface within 60 studs.",
		icon = "", rarity = "uncommon", maxOwned = 1,
	},
	{
		id = "medkit", name = "Field Medkit", price = 120, tab = "gear",
		description = "Restores 50 health over 4 seconds. Stackable.",
		icon = "", rarity = "common", maxOwned = 10,
	},
	{
		id = "double_cash", name = "2× Cash (30m)", price = 1500, tab = "boosts",
		description = "Doubles every Cash pickup for 30 minutes.",
		icon = "", rarity = "rare", maxOwned = 3,
	},
	{
		id = "swift_foot", name = "Swift Foot (10m)", price = 600, tab = "boosts",
		description = "+30% WalkSpeed for 10 minutes.",
		icon = "", rarity = "uncommon", maxOwned = 5,
	},
	{
		id = "emerald_trail", name = "Emerald Trail", price = 2200, tab = "cosmetics",
		description = "A green trail that follows your character.",
		icon = "", rarity = "rare", maxOwned = 1,
	},
	{
		id = "neon_nameplate", name = "Neon Nameplate", price = 3000, tab = "cosmetics",
		description = "Your name glows above your head in the accent colour.",
		icon = "", rarity = "legendary", maxOwned = 1,
	},
}

local CONFIG: ShopData = {
	title = "Shop",
	currencyName = "Cash",
	startingCash = 500,
	maxPurchaseQuantity = 10,
	purchaseCooldown = 0.4,
	rateLimitWindow = 5,
	rateLimitMax = 8,
	toggleKey = Enum.KeyCode.B,
	openOnSpawn = false,
	tabs = TABS,
	items = ITEMS,
	palette = PALETTE,
	-- Empty strings mean "silent": the client only plays a sound when an id is
	-- set, so this never fires a made-up asset id at the client.
	sounds = { hover = "", click = "", success = "", denied = "" },
}

-- ── Pure helpers (safe on both sides) ───────────────────────────────────────
local ShopConfig = {}

ShopConfig.data = CONFIG

function ShopConfig.getItem(itemId: unknown): ItemData?
	if typeof(itemId) ~= "string" then
		return nil
	end
	for _, item in CONFIG.items do
		if item.id == itemId then
			return item
		end
	end
	return nil
end

function ShopConfig.sortedTabs(): { TabData }
	local copy: { TabData } = {}
	for _, tab in CONFIG.tabs do
		table.insert(copy, tab)
	end
	table.sort(copy, function(a: TabData, b: TabData): boolean
		return a.order < b.order
	end)
	return copy
end

function ShopConfig.itemsInTab(tabId: string): { ItemData }
	local out: { ItemData } = {}
	for _, item in CONFIG.items do
		if item.tab == tabId then
			table.insert(out, item)
		end
	end
	table.sort(out, function(a: ItemData, b: ItemData): boolean
		return a.price < b.price
	end)
	return out
end

-- The price the SERVER will charge. The client may call this to draw a label,
-- but the server calls it again at purchase time and trusts only that result.
function ShopConfig.priceOf(itemId: string): number
	local item = ShopConfig.getItem(itemId)
	return if item then item.price else -1
end

function ShopConfig.formatPrice(amount: number): string
	return string.format("%s %s", tostring(math.floor(amount + 0.5)), CONFIG.currencyName)
end

return ShopConfig
