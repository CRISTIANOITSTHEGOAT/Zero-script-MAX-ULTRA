--!strict
-- ══════════════════════════════════════════════════════════════════════════
--  ShopServer  ·  Script  ·  game.ServerScriptService.ShopServer
-- ══════════════════════════════════════════════════════════════════════════
--  Owns the truth: currency, inventory, prices, and every purchase decision.
--  The client only ever asks; nothing it sends is believed beyond an item id
--  and a quantity, both re-checked against ShopConfig here.
--
--  Also creates the remotes itself (ReplicatedStorage.ShopRemotes), so the
--  system needs no manual instance setup in the place.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local DataStoreService = game:GetService("DataStoreService")
local RunService = game:GetService("RunService")
local CollectionService = game:GetService("CollectionService")

local ShopConfig = require(ReplicatedStorage:WaitForChild("ShopConfig", 10))
local CONFIG = ShopConfig.data

-- ── Types ───────────────────────────────────────────────────────────────────
type Profile = {
	cash: number,
	inventory: { [string]: number },
}

type Session = {
	profile: Profile,
	loaded: boolean,
	saveAttempts: number,
	lastPurchase: number,
	attempts: { number },
	violations: number,
	key: string,
}

-- ── Remotes (created here so a fresh place works with one script) ───────────
local function makeRemotes(): Instance
	local folder = Instance.new("Folder")
	folder.Name = "ShopRemotes"

	local purchase = Instance.new("RemoteEvent")
	purchase.Name = "PurchaseItem"
	purchase.Parent = folder

	local result = Instance.new("RemoteEvent")
	result.Name = "PurchaseResult"
	result.Parent = folder

	local currency = Instance.new("RemoteEvent")
	currency.Name = "CurrencyChanged"
	currency.Parent = folder

	local inventoryChanged = Instance.new("RemoteEvent")
	inventoryChanged.Name = "InventoryChanged"
	inventoryChanged.Parent = folder

	local requestData = Instance.new("RemoteFunction")
	requestData.Name = "RequestData"
	requestData.Parent = folder

	folder.Parent = ReplicatedStorage        -- Parent LAST: one replication, not six
	return folder
end

local remotes = ReplicatedStorage:FindFirstChild("ShopRemotes") or makeRemotes()
local purchaseRemote = remotes:WaitForChild("PurchaseItem", 10) :: RemoteEvent
local resultRemote = remotes:WaitForChild("PurchaseResult", 10) :: RemoteEvent
local currencyRemote = remotes:WaitForChild("CurrencyChanged", 10) :: RemoteEvent
local inventoryRemote = remotes:WaitForChild("InventoryChanged", 10) :: RemoteEvent
local requestFunction = remotes:WaitForChild("RequestData", 10) :: RemoteFunction

-- ── Persistence ─────────────────────────────────────────────────────────────
local STORE_NAME = "ZeroScriptShop_v1"
local store = DataStoreService:GetDataStore(STORE_NAME)
local sessions: { [Player]: Session } = {}

local function defaultProfile(): Profile
	return { cash = CONFIG.startingCash, inventory = {} }
end

-- A DataStore call that survives the occasional throttle/timeout: three tries
-- with a growing pause, because one dropped save is a player losing their Cash.
local function withRetry<T>(label: string, fn: () -> T): (boolean, T?)
	local delayFor = 0.5
	for attempt = 1, 3 do
		local ok, value = pcall(fn)
		if ok then
			return true, value :: T
		end
		warn(string.format("[ShopServer] %s failed (attempt %d/3): %s", label, attempt, tostring(value)))
		if attempt < 3 then
			task.wait(delayFor)
			delayFor *= 2
		end
	end
	return false, nil
end

local function coerceProfile(raw: unknown): Profile
	local profile = defaultProfile()
	if typeof(raw) ~= "table" then
		return profile
	end
	local table_ = raw :: { [string]: unknown }
	if typeof(table_.cash) == "number" and (table_.cash :: number) >= 0 then
		profile.cash = math.floor(table_.cash :: number)
	end
	if typeof(table_.inventory) == "table" then
		for itemId, count in table_.inventory :: { [unknown]: unknown } do
			if typeof(itemId) == "string" and typeof(count) == "number" then
				profile.inventory[itemId] = math.clamp(math.floor(count :: number), 0, 999)
			end
		end
	end
	return profile
end

local function pushState(player: Player, session: Session)
	currencyRemote:FireClient(player, session.profile.cash)
	inventoryRemote:FireClient(player, session.profile.inventory)
end

local function loadPlayer(player: Player)
	local key = "player_" .. tostring(player.UserId)
	local ok, raw = withRetry("load " .. key, function()
		return store:GetAsync(key)
	end)
	local profile = if ok then coerceProfile(raw) else defaultProfile()

	local session: Session = {
		profile = profile,
		loaded = ok,
		saveAttempts = 0,
		lastPurchase = 0,
		attempts = {},
		violations = 0,
		key = key,
	}
	sessions[player] = session

	-- leaderstats: the currency is visible in the player list, and other
	-- systems can read it without touching this script's internals.
	local leaderstats = Instance.new("Folder")
	leaderstats.Name = "leaderstats"
	local cash = Instance.new("IntValue")
	cash.Name = CONFIG.currencyName
	cash.Value = session.profile.cash
	cash.Parent = leaderstats
	leaderstats.Parent = player

	pushState(player, session)
	if not ok then
		warn("[ShopServer] " .. player.Name .. " loaded with defaults - the DataStore did not answer.")
	end
end

local function savePlayer(player: Player)
	local session = sessions[player]
	if not session then
		return
	end
	local key = session.key
	local snapshot = { cash = session.profile.cash, inventory = session.profile.inventory }
	local ok = withRetry("save " .. key, function()
		store:SetAsync(key, snapshot)
		return true
	end)
	if not ok then
		warn("[ShopServer] could not save " .. player.Name .. " - their next session may roll back.")
	end
end

-- ── Anti-exploit bookkeeping ────────────────────────────────────────────────
local function flag(player: Player, session: Session, reason: string)
	session.violations += 1
	warn(string.format("[ShopServer] refused %s (%s) - %s", player.Name, reason,
		session.violations == 1 and "first refusal" or (tostring(session.violations) .. " refusals so far")))
end

-- A rolling window: drop the timestamps older than the window, then count what
-- is left. Catches a client firing PurchaseItem in a loop.
local function rateLimited(session: Session, now: number): boolean
	local window = CONFIG.rateLimitWindow
	local kept: { number } = {}
	for _, at in session.attempts do
		if now - at <= window then
			table.insert(kept, at)
		end
	end
	session.attempts = kept
	return #session.attempts >= CONFIG.rateLimitMax
end

local function normaliseQuantity(raw: unknown): number
	if typeof(raw) ~= "number" then
		return 1
	end
	local n = raw :: number
	if n ~= n or n == math.huge or n == -math.huge then       -- NaN / inf guard
		return 1
	end
	return math.clamp(math.floor(n), 1, CONFIG.maxPurchaseQuantity)
end

-- ── What owning an item actually does ───────────────────────────────────────
-- Cosmetics and boosts are applied here; gear is recorded and announced, so a
-- combat/movement system can grant the real Tool from ItemGranted without this
-- script needing to know how weapons work.
local BOOST_SECONDS: { [string]: number } = {
	swift_foot = 600,
	double_cash = 1800,
}

local function applyToCharacter(player: Player, character: Model)
	local session = sessions[player]
	if not session then
		return
	end
	local rootPart = character:WaitForChild("HumanoidRootPart", 10) :: BasePart?
	local humanoid = character:WaitForChild("Humanoid", 10) :: Humanoid?

	if session.profile.inventory["emerald_trail"] and rootPart then
		local a0 = Instance.new("Attachment")
		a0.Name = "ShopTrailA"
		a0.Position = Vector3.new(0, 1, 0)
		a0.Parent = rootPart
		local a1 = Instance.new("Attachment")
		a1.Name = "ShopTrailB"
		a1.Position = Vector3.new(0, -1, 0)
		a1.Parent = rootPart
		local trail = Instance.new("Trail")
		trail.Name = "ShopTrail"
		trail.Attachment0 = a0
		trail.Attachment1 = a1
		trail.Color = ColorSequence.new(CONFIG.palette.accent, CONFIG.palette.accentDim)
		trail.Lifetime = 0.9
		trail.Parent = rootPart
	end

	if session.profile.inventory["neon_nameplate"] and rootPart then
		local billboard = Instance.new("BillboardGui")
		billboard.Name = "ShopNameplate"
		-- The one pixel-sized UDim2 in this system, and it is correct: a
		-- BillboardGui lives in world space, where Scale would mean "a fraction of
		-- the screen" and stretch the nameplate with the camera. Screen GUIs are
		-- Scale-only; this is not one.
		billboard.Size = UDim2.new(0, 190, 0, 34)
		billboard.StudsOffset = Vector3.new(0, 3, 0)
		billboard.AlwaysOnTop = true
		billboard.Adornee = rootPart
		local label = Instance.new("TextLabel")
		label.Name = "Name"
		label.BackgroundTransparency = 1
		label.Size = UDim2.new(1, 0, 1, 0)
		label.Font = Enum.Font.GothamBold
		label.TextSize = 20
		label.TextColor3 = CONFIG.palette.accent
		label.TextStrokeTransparency = 0.4
		label.Text = player.DisplayName
		label.Parent = billboard
		billboard.Parent = rootPart
	end

	if session.profile.inventory["swift_foot"] and humanoid then
		humanoid.WalkSpeed *= 1.3
	end
	if session.profile.inventory["double_cash"] then
		-- A TAG, not a flag in this script: any system that pays out Cash can
		-- check CollectionService:HasTag(player, "ShopDoubleCash") and double it.
		CollectionService:AddTag(player, "ShopDoubleCash")
	end
end

local function startBoostTimer(player: Player, itemId: string)
	local seconds = BOOST_SECONDS[itemId]
	if not seconds then
		return
	end
	task.delay(seconds, function()
		local session = sessions[player]
		if not session then
			return
		end
		session.profile.inventory[itemId] = nil
		if itemId == "double_cash" then
			CollectionService:RemoveTag(player, "ShopDoubleCash")
		end
		inventoryRemote:FireClient(player, session.profile.inventory)
		resultRemote:FireClient(player, true, "Your " .. itemId .. " boost expired.", itemId, 0)
	end)
end

-- ── Purchase: the only path that moves money ────────────────────────────────
local function onPurchase(player: Player, rawItemId: unknown, rawQuantity: unknown)
	local session = sessions[player]
	if not session then
		return
	end
	local now = os.clock()

	-- 1. Shape. Anything that is not a string id is a client bug or an exploit.
	if typeof(rawItemId) ~= "string" then
		flag(player, session, "item id was " .. typeof(rawItemId))
		return
	end
	local itemId = rawItemId :: string
	local item = ShopConfig.getItem(itemId)
	if not item then
		flag(player, session, "unknown item " .. itemId)
		return
	end

	-- 2. Rate limit and cooldown, before any work is done.
	if rateLimited(session, now) then
		flag(player, session, "rate limit")
		resultRemote:FireClient(player, false, "Too many requests - wait a moment.", itemId, 0)
		return
	end
	if now - session.lastPurchase < CONFIG.purchaseCooldown then
		resultRemote:FireClient(player, false, "Slow down.", itemId, 0)
		return
	end
	table.insert(session.attempts, now)
	session.lastPurchase = now

	-- 3. Quantity is clamped here, never trusted from the client.
	local quantity = normaliseQuantity(rawQuantity)
	local owned = session.profile.inventory[itemId] or 0
	if item.maxOwned > 0 and owned + quantity > item.maxOwned then
		quantity = math.max(0, item.maxOwned - owned)
		if quantity == 0 then
			resultRemote:FireClient(player, false, "You already own the most you can of " .. item.name .. ".",
				itemId, 0)
			return
		end
	end

	-- 4. Price comes from the config on the SERVER. Integer maths only.
	local total = math.floor(item.price * quantity)
	if session.profile.cash < total then
		resultRemote:FireClient(player, false,
			string.format("Not enough %s: %s costs %d, you have %d.",
				CONFIG.currencyName, item.name, total, session.profile.cash), itemId, quantity)
		return
	end

	-- 5. Commit, then persist with UpdateAsync so two servers can't race.
	session.profile.cash -= total
	session.profile.inventory[itemId] = owned + quantity

	local leaderstats = player:FindFirstChild("leaderstats")
	local cashValue = leaderstats and leaderstats:FindFirstChild(CONFIG.currencyName)
	if cashValue and cashValue:IsA("IntValue") then
		cashValue.Value = session.profile.cash
	end

	local key = session.key
	local snapshot = { cash = session.profile.cash, inventory = session.profile.inventory }
	task.spawn(function()
		withRetry("purchase " .. key, function()
			store:UpdateAsync(key, function()
				return snapshot
			end)
			return true
		end)
	end)

	pushState(player, session)
	startBoostTimer(player, itemId)
	if player.Character then
		applyToCharacter(player, player.Character)
	end

	resultRemote:FireClient(player, true,
		string.format("Bought %d × %s for %d %s.", quantity, item.name, total, CONFIG.currencyName),
		itemId, quantity)
	print(string.format("[ShopServer] %s bought %d × %s for %d", player.Name, quantity, itemId, total))
end

-- ── Wiring ──────────────────────────────────────────────────────────────────
purchaseRemote.OnServerEvent:Connect(onPurchase)

requestFunction.OnServerInvoke = function(player: Player): { [string]: unknown }
	local session = sessions[player]
	if not session then
		return { cash = 0, inventory = {}, loaded = false }
	end
	return {
		cash = session.profile.cash,
		inventory = session.profile.inventory,
		loaded = session.loaded,
		currencyName = CONFIG.currencyName,
	}
end

Players.PlayerAdded:Connect(function(player: Player)
	loadPlayer(player)
	player.CharacterAdded:Connect(function(character: Model)
		applyToCharacter(player, character)
	end)
	if player.Character then
		applyToCharacter(player, player.Character)
	end
end)

Players.PlayerRemoving:Connect(function(player: Player)
	savePlayer(player)
	sessions[player] = nil
end)

-- Studio stops the server without warning; BindToClose is what makes the last
-- purchase stick instead of rolling back to the previous session.
game:BindToClose(function()
	if RunService:IsStudio() then
		task.wait(0.5)
	end
	for _, player in Players:GetPlayers() do
		savePlayer(player)
	end
end)

for _, player in Players:GetPlayers() do
	task.spawn(loadPlayer, player)
end

print("[ShopServer] ready - " .. tostring(#CONFIG.items) .. " items across "
	.. tostring(#CONFIG.tabs) .. " tabs, remotes in ReplicatedStorage.ShopRemotes")
