--!strict
-- ══════════════════════════════════════════════════════════════════════════
--  ShopClient  ·  LocalScript  ·  game.StarterGui.ShopClient
-- ══════════════════════════════════════════════════════════════════════════
--  Draws the shop and asks the server for things. It decides NOTHING about
--  price or ownership: every card is rendered from ShopConfig, every purchase
--  is a request, and the truth arrives back on PurchaseResult.
--
--  Layout is Scale-only, so it holds on mobile, PC and console; UISizeConstraint
--  and UIAspectRatioConstraint on the root keep it inside sensible bounds on a
--  4K monitor and a small phone.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TweenService = game:GetService("TweenService")
local UserInputService = game:GetService("UserInputService")
local SoundService = game:GetService("SoundService")
local CollectionService = game:GetService("CollectionService")

local player = Players.LocalPlayer
local ShopConfig = require(ReplicatedStorage:WaitForChild("ShopConfig", 10))
local CONFIG = ShopConfig.data
local PALETTE = CONFIG.palette

local remotes = ReplicatedStorage:WaitForChild("ShopRemotes", 20)
local purchaseRemote = remotes:WaitForChild("PurchaseItem", 10) :: RemoteEvent
local resultRemote = remotes:WaitForChild("PurchaseResult", 10) :: RemoteEvent
local currencyRemote = remotes:WaitForChild("CurrencyChanged", 10) :: RemoteEvent
local inventoryRemote = remotes:WaitForChild("InventoryChanged", 10) :: RemoteEvent
local requestFunction = remotes:WaitForChild("RequestData", 10) :: RemoteFunction

local CARD_TAG = "ShopCard"
local HOVER_SCALE = 1.05
local PRESS_SCALE = 0.97

-- ── State ───────────────────────────────────────────────────────────────────
local cash: number = 0
local inventory: { [string]: number } = {}
local openTab: string = ShopConfig.sortedTabs()[1].id
local isOpen = false
local busy = false                       -- one purchase in flight at a time
local busyToken = 0                      -- which request owns `busy` right now

-- Declared here, defined at the bottom: rendering a card connects its buy
-- button to it, and a finished purchase re-renders the cards. One of the two
-- has to be forward-declared or the other is a nil global at call time.
local requestPurchase: (itemId: string, quantity: number) -> ()
local quantityFor: (item: ShopConfig.ItemData) -> number

-- ── Small builders (Parent is always set LAST) ──────────────────────────────
local function corner(parent: Instance, radius: number)
	local c = Instance.new("UICorner")
	c.CornerRadius = UDim.new(0, radius)
	c.Parent = parent
	return c
end

local function stroke(parent: Instance, color: Color3, transparency: number, thickness: number)
	local s = Instance.new("UIStroke")
	s.Color = color
	s.Transparency = transparency
	s.Thickness = thickness
	s.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
	s.Parent = parent
	return s
end

local function padding(parent: Instance, px: number)
	local p = Instance.new("UIPadding")
	p.PaddingTop = UDim.new(0, px)
	p.PaddingBottom = UDim.new(0, px)
	p.PaddingLeft = UDim.new(0, px)
	p.PaddingRight = UDim.new(0, px)
	p.Parent = parent
	return p
end

local function label(text: string, size: number, color: Color3, font: Enum.Font): TextLabel
	local l = Instance.new("TextLabel")
	l.BackgroundTransparency = 1
	l.Text = text
	l.TextSize = size
	l.TextColor3 = color
	l.Font = font
	l.TextScaled = false
	l.RichText = true
	return l
end

-- Sounds only exist when the config supplies a real id, so this never fires a
-- invented asset at the client.
local function playSound(id: string, volume: number)
	if id == "" then
		return
	end
	local s = Instance.new("Sound")
	s.SoundId = id
	s.Volume = volume
	s.Parent = SoundService
	s:Play()
	s.Ended:Connect(function()
		s:Destroy()
	end)
	task.delay(6, function()
		if s.Parent then
			s:Destroy()
		end
	end)
end

-- ── Micro-interactions ──────────────────────────────────────────────────────
-- A UIScale is tweened rather than Size: the cards live in a UIGridLayout, and
-- tweening Size would fight the layout on every frame.
local function bindInteractions(card: GuiButton)
	local scale = card:FindFirstChildOfClass("UIScale")
	if not scale then
		return
	end
	local tweenInfo = TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out)

	local function to(value: number)
		local tween = TweenService:Create(scale, tweenInfo, { Scale = value })
		tween:Play()
	end

	card.MouseEnter:Connect(function()
		to(HOVER_SCALE)
		playSound(CONFIG.sounds.hover, 0.35)
	end)
	card.MouseLeave:Connect(function()
		to(1)
	end)
	card.SelectionEnter:Connect(function()      -- gamepad / console
		to(HOVER_SCALE)
	end)
	card.SelectionLeave:Connect(function()
		to(1)
	end)
	card.InputBegan:Connect(function(input: InputObject)  -- touch
		if input.UserInputType == Enum.UserInputType.Touch then
			to(HOVER_SCALE)
		end
	end)
	card.InputEnded:Connect(function(input: InputObject)
		if input.UserInputType == Enum.UserInputType.Touch then
			to(1)
		end
	end)
	card.MouseButton1Down:Connect(function()
		to(PRESS_SCALE)
		playSound(CONFIG.sounds.click, 0.5)
	end)
	card.MouseButton1Up:Connect(function()
		to(HOVER_SCALE)
	end)
end

-- Cards are tagged rather than kept in a list, so anything added later (a
-- rebuilt tab, a seasonal item) gets its interactions from one place.
CollectionService:GetInstanceAddedSignal(CARD_TAG):Connect(function(inst: Instance)
	local card = inst :: GuiButton
	task.spawn(bindInteractions, card)
end)

-- ── GUI construction ────────────────────────────────────────────────────────
local screenGui = Instance.new("ScreenGui")
screenGui.Name = "ShopClient"
screenGui.ResetOnSpawn = false
screenGui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling
screenGui.DisplayOrder = 20
screenGui.IgnoreGuiInset = false

local toggle = Instance.new("TextButton")
toggle.Name = "Toggle"
toggle.AnchorPoint = Vector2.new(1, 1)
-- Corner-pinned: the MARGIN is pixels (a position, not a size), the button
-- itself is Scale and then clamped, so it stays a usable tap target everywhere.
toggle.Position = UDim2.new(1, -16, 1, -16)
toggle.Size = UDim2.new(0.062, 0, 0.062, 0)      -- square in scale, both axes
toggle.BackgroundColor3 = PALETTE.accent
toggle.Text = "🛒"
toggle.TextSize = 22
toggle.Font = Enum.Font.GothamBold
toggle.TextColor3 = PALETTE.textPrimary
toggle.AutoButtonColor = false
corner(toggle, 10)
stroke(toggle, PALETTE.border, 0.7, 1)
local toggleAspect = Instance.new("UIAspectRatioConstraint")
toggleAspect.AspectRatio = 1
toggleAspect.Parent = toggle
local toggleMax = Instance.new("UISizeConstraint")
toggleMax.MaxSize = Vector2.new(72, 72)
toggleMax.MinSize = Vector2.new(44, 44)
toggleMax.Parent = toggle
toggle.Parent = screenGui

local root = Instance.new("Frame")
root.Name = "Root"
root.AnchorPoint = Vector2.new(0.5, 0.5)
root.Position = UDim2.new(0.5, 0, 0.5, 0)
root.Size = UDim2.new(0.62, 0, 0.72, 0)           -- Scale only, no pixel sizing
root.BackgroundColor3 = PALETTE.background
root.BorderSizePixel = 0
root.Visible = false
corner(root, 12)
stroke(root, PALETTE.border, 0.7, 1)
padding(root, 12)
local rootAspect = Instance.new("UIAspectRatioConstraint")
rootAspect.AspectRatio = 1.55
rootAspect.AspectType = Enum.AspectType.FitWithinMaxSize
rootAspect.Parent = root
local rootSize = Instance.new("UISizeConstraint")
rootSize.MaxSize = Vector2.new(1040, 700)
rootSize.MinSize = Vector2.new(320, 240)
rootSize.Parent = root
root.Parent = screenGui

local header = Instance.new("Frame")
header.Name = "Header"
header.Size = UDim2.new(1, 0, 0.09, 0)
header.BackgroundTransparency = 1
header.Parent = root

local title = label(CONFIG.title, 24, PALETTE.textPrimary, Enum.Font.GothamBold)
title.Name = "Title"
title.Size = UDim2.new(0.5, 0, 1, 0)
title.TextXAlignment = Enum.TextXAlignment.Left
title.Parent = header

local cashLabel = label("", 20, PALETTE.accent, Enum.Font.GothamBold)
cashLabel.Name = "Cash"
cashLabel.AnchorPoint = Vector2.new(1, 0)
cashLabel.Position = UDim2.new(1, -44, 0, 0)     -- clear of the close button
cashLabel.Size = UDim2.new(0.3, 0, 1, 0)
cashLabel.TextXAlignment = Enum.TextXAlignment.Right
cashLabel.Parent = header

local closeButton = Instance.new("TextButton")
closeButton.Name = "Close"
closeButton.AnchorPoint = Vector2.new(1, 0.5)
closeButton.Position = UDim2.new(1, 0, 0.5, 0)
closeButton.Size = UDim2.new(0.05, 0, 0.62, 0)   -- Scale, like every size here
closeButton.BackgroundColor3 = PALETTE.card
closeButton.Text = "✕"
closeButton.TextSize = 16
closeButton.Font = Enum.Font.GothamBold
closeButton.TextColor3 = PALETTE.textSecondary
closeButton.AutoButtonColor = false
corner(closeButton, 8)
stroke(closeButton, PALETTE.border, 0.7, 1)
-- Scale alone would make this 13px tall in a small window, so it is sized in
-- Scale and CLAMPED in pixels (the toggle button's pattern) rather than sized in
-- pixels: a close button that shrinks below a tap target is a bug, and one that
-- grows to 60px on a 4K screen is another.
local closeAspect = Instance.new("UIAspectRatioConstraint")
closeAspect.AspectRatio = 1
closeAspect.Parent = closeButton
local closeSize = Instance.new("UISizeConstraint")
closeSize.MinSize = Vector2.new(24, 24)
closeSize.MaxSize = Vector2.new(34, 34)
closeSize.Parent = closeButton
closeButton.Parent = header

local tabBar = Instance.new("Frame")
tabBar.Name = "Tabs"
tabBar.Position = UDim2.new(0, 0, 0.1, 0)
tabBar.Size = UDim2.new(1, 0, 0.07, 0)
tabBar.BackgroundTransparency = 1
tabBar.Parent = root

local tabLayout = Instance.new("UIListLayout")
tabLayout.FillDirection = Enum.FillDirection.Horizontal
tabLayout.HorizontalAlignment = Enum.HorizontalAlignment.Left
tabLayout.VerticalAlignment = Enum.VerticalAlignment.Center
tabLayout.SortOrder = Enum.SortOrder.LayoutOrder
tabLayout.Padding = UDim.new(0, 8)
tabLayout.Parent = tabBar

local scroll = Instance.new("ScrollingFrame")
scroll.Name = "Items"
scroll.Position = UDim2.new(0, 0, 0.19, 0)
scroll.Size = UDim2.new(1, 0, 0.73, 0)
scroll.BackgroundTransparency = 1
scroll.BorderSizePixel = 0
scroll.ScrollBarThickness = 4
scroll.ScrollBarImageColor3 = PALETTE.border
scroll.AutomaticCanvasSize = Enum.AutomaticSize.Y
scroll.CanvasSize = UDim2.new(0, 0, 0, 0)
scroll.Parent = root

local grid = Instance.new("UIGridLayout")
grid.CellSize = UDim2.new(0.315, 0, 0.30, 0)      -- Scale cells: 3 across
grid.CellPadding = UDim2.new(0.025, 0, 0.03, 0)
grid.SortOrder = Enum.SortOrder.LayoutOrder
grid.Parent = scroll

local scrollPadding = padding(scroll, 4)
scrollPadding.PaddingTop = UDim.new(0, 6)

local status = label("", 15, PALETTE.textSecondary, Enum.Font.Gotham)
status.Name = "Status"
status.Position = UDim2.new(0, 0, 0.93, 0)
status.Size = UDim2.new(1, 0, 0.07, 0)
status.TextXAlignment = Enum.TextXAlignment.Center
status.TextWrapped = true
status.Parent = root

screenGui.Parent = player:WaitForChild("PlayerGui", 10)

-- ── Rendering ───────────────────────────────────────────────────────────────
local function setStatus(text: string, color: Color3?)
	status.Text = text
	status.TextColor3 = color or PALETTE.textSecondary
end

local function refreshCash()
	cashLabel.Text = "💰 " .. ShopConfig.formatPrice(cash)
end

local function ownedOf(itemId: string): number
	return inventory[itemId] or 0
end

local function buildCard(item: ShopConfig.ItemData, order: number): GuiButton
	local owned = ownedOf(item.id)
	local maxed = item.maxOwned > 0 and owned >= item.maxOwned
	local affordable = cash >= item.price

	local card = Instance.new("TextButton")
	card.Name = item.id
	card.LayoutOrder = order
	card.BackgroundColor3 = PALETTE.card
	card.AutoButtonColor = false
	card.Text = ""
	card.BorderSizePixel = 0
	card.ClipsDescendants = true
	corner(card, 8)
	stroke(card, PALETTE.rarity[item.rarity], 0.55, 1)
	padding(card, 10)

	local scale = Instance.new("UIScale")     -- the tween target for hover/press
	scale.Scale = 1
	scale.Parent = card

	local accentBar = Instance.new("Frame")
	accentBar.Name = "Rarity"
	accentBar.Size = UDim2.new(1, 0, 0.035, 0)
	accentBar.BackgroundColor3 = PALETTE.rarity[item.rarity]
	accentBar.BorderSizePixel = 0
	accentBar.Parent = card
	corner(accentBar, 4)

	local iconHolder = Instance.new("Frame")
	iconHolder.Name = "Icon"
	iconHolder.Position = UDim2.new(0, 0, 0.08, 0)
	iconHolder.Size = UDim2.new(1, 0, 0.4, 0)
	iconHolder.BackgroundColor3 = PALETTE.background
	iconHolder.BorderSizePixel = 0
	iconHolder.Parent = card
	corner(iconHolder, 6)

	if item.icon ~= "" then
		local image = Instance.new("ImageLabel")
		image.Name = "Image"
		image.BackgroundTransparency = 1
		image.Size = UDim2.new(0.8, 0, 0.8, 0)
		image.Position = UDim2.new(0.1, 0, 0.1, 0)
		image.Image = item.icon
		image.ScaleType = Enum.ScaleType.Fit
		image.Parent = iconHolder
	else
		-- No art yet: show the initials rather than an empty grey box.
		local initials = label(string.upper(string.sub(item.name, 1, 1)
			.. string.sub(item.name:find(" ") and item.name:match("%a+$") or "")),
			22, PALETTE.rarity[item.rarity], Enum.Font.GothamBold)
		initials.Name = "Initials"
		initials.Size = UDim2.new(1, 0, 1, 0)
		initials.Parent = iconHolder
	end

	local nameLabel = label(item.name, 16, PALETTE.textPrimary, Enum.Font.GothamBold)
	nameLabel.Name = "Name"
	nameLabel.Position = UDim2.new(0, 0, 0.52, 0)
	nameLabel.Size = UDim2.new(1, 0, 0.14, 0)
	nameLabel.TextXAlignment = Enum.TextXAlignment.Left
	nameLabel.TextTruncate = Enum.TextTruncate.AtEnd
	nameLabel.Parent = card

	local descLabel = label(item.description, 12, PALETTE.textSecondary, Enum.Font.Gotham)
	descLabel.Name = "Description"
	descLabel.Position = UDim2.new(0, 0, 0.66, 0)
	descLabel.Size = UDim2.new(1, 0, 0.16, 0)
	descLabel.TextXAlignment = Enum.TextXAlignment.Left
	descLabel.TextYAlignment = Enum.TextYAlignment.Top
	descLabel.TextWrapped = true
	descLabel.TextTruncate = Enum.TextTruncate.AtEnd
	descLabel.Parent = card

	local buy = Instance.new("TextButton")
	buy.Name = "Buy"
	buy.Position = UDim2.new(0, 0, 0.84, 0)
	buy.Size = UDim2.new(1, 0, 0.16, 0)
	buy.BackgroundColor3 = if maxed then PALETTE.border
		else if affordable then PALETTE.accent else PALETTE.card
	buy.Text = if maxed then "OWNED"
		else if affordable then ("Buy · " .. tostring(item.price)) else ("Need " .. tostring(item.price))
	buy.TextSize = 14
	buy.Font = Enum.Font.GothamBold
	buy.TextColor3 = if affordable and not maxed then PALETTE.background else PALETTE.textSecondary
	buy.AutoButtonColor = false
	buy.BorderSizePixel = 0
	buy.Parent = card
	corner(buy, 6)

	local ownedLabel = label(owned > 0 and ("Owned: " .. tostring(owned)) or "", 11,
		PALETTE.textSecondary, Enum.Font.Gotham)
	ownedLabel.Name = "Owned"
	ownedLabel.AnchorPoint = Vector2.new(1, 0)
	ownedLabel.Position = UDim2.new(1, 0, 0.08, 0)
	ownedLabel.Size = UDim2.new(0.5, 0, 0.08, 0)
	ownedLabel.TextXAlignment = Enum.TextXAlignment.Right
	ownedLabel.Parent = card

	CollectionService:AddTag(card, CARD_TAG)
	return card
end

local function renderTab()
	for _, child in scroll:GetChildren() do
		if child:IsA("GuiButton") then
			CollectionService:RemoveTag(child, CARD_TAG)
			child:Destroy()
		end
	end
	local order = 0
	for _, item in ShopConfig.itemsInTab(openTab) do
		order += 1
		local card = buildCard(item, order)
		card.Parent = scroll
		bindInteractions(card)
		card.Activated:Connect(function()
			requestPurchase(item.id, quantityFor(item))
		end)
	end
	if order == 0 then
		setStatus("Nothing in this tab yet.", PALETTE.textSecondary)
	end
end

local function renderTabs()
	for _, child in tabBar:GetChildren() do
		if child:IsA("TextButton") then
			child:Destroy()
		end
	end
	for index, tab in ShopConfig.sortedTabs() do
		local button = Instance.new("TextButton")
		button.Name = tab.id
		button.LayoutOrder = index
		button.Size = UDim2.new(0.18, 0, 0.85, 0)
		button.BackgroundColor3 = if tab.id == openTab then PALETTE.card else PALETTE.background
		button.Text = tab.label
		button.TextSize = 15
		button.Font = Enum.Font.GothamBold
		button.TextColor3 = if tab.id == openTab then PALETTE.accent else PALETTE.textSecondary
		button.AutoButtonColor = false
		button.BorderSizePixel = 0
		corner(button, 8)
		stroke(button, if tab.id == openTab then PALETTE.accent else PALETTE.border, 0.7, 1)
		button.Parent = tabBar
		button.Activated:Connect(function()
			if openTab == tab.id then
				return
			end
			openTab = tab.id
			playSound(CONFIG.sounds.click, 0.4)
			renderTabs()
			renderTab()
		end)
	end
end

-- ── Open / close ────────────────────────────────────────────────────────────
local function setOpen(wantOpen: boolean)
	if isOpen == wantOpen then
		return
	end
	isOpen = wantOpen
	root.Visible = true
	local target = if wantOpen then 1 else 0.92
	TweenService:Create(root, TweenInfo.new(0.22, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
		{ Size = UDim2.new(0.62 * target, 0, 0.72 * target, 0) }):Play()
	if wantOpen then
		renderTabs()
		renderTab()
		setStatus("Pick an item - the server confirms every purchase.", PALETTE.textSecondary)
	else
		task.delay(0.24, function()
			if not isOpen then
				root.Visible = false
			end
		end)
	end
	playSound(CONFIG.sounds.click, 0.4)
end

toggle.Activated:Connect(function()
	setOpen(not isOpen)
end)
closeButton.Activated:Connect(function()
	setOpen(false)
end)

UserInputService.InputBegan:Connect(function(input: InputObject, processed: boolean)
	if processed then
		return
	end
	if input.KeyCode == CONFIG.toggleKey then
		setOpen(not isOpen)
	elseif input.KeyCode == Enum.KeyCode.Escape and isOpen then
		setOpen(false)
	end
end)

-- ── Purchasing ──────────────────────────────────────────────────────────────
requestPurchase = function(itemId: string, quantity: number)
	if busy then
		return
	end
	busy = true
	busyToken += 1
	local token = busyToken
	setStatus("Asking the server...", PALETTE.textSecondary)
	purchaseRemote:FireServer(itemId, quantity)
	-- The answer comes back on PurchaseResult. If it never does (a dropped
	-- packet, a server hiccup) this watchdog frees the button, so one lost
	-- reply cannot leave the shop permanently un-clickable. The token check is
	-- what stops a LATE watchdog from unlocking a newer request that is still
	-- waiting on its own reply.
	task.delay(8, function()
		if token == busyToken then
			busy = false
		end
	end)
end

resultRemote.OnClientEvent:Connect(function(ok: boolean, message: unknown, itemId: unknown, quantity: unknown)
	busy = false
	busyToken += 1                         -- retire any watchdog still pending
	local text = if typeof(message) == "string" then message :: string else "Purchase finished."
	if ok then
		setStatus(text, PALETTE.accent)
		playSound(CONFIG.sounds.success, 0.6)
	else
		setStatus(text, PALETTE.danger)
		playSound(CONFIG.sounds.denied, 0.6)
	end
	-- The server has already pushed the new Cash and inventory by now; redraw so
	-- the card shows its new OWNED / affordability state.
	if isOpen then
		renderTab()
	end
	if typeof(itemId) == "string" and typeof(quantity) == "number" and ok then
		print(string.format("[ShopClient] confirmed %d x %s", quantity :: number, itemId :: string))
	end
end)

currencyRemote.OnClientEvent:Connect(function(value: unknown)
	if typeof(value) == "number" then
		cash = math.max(0, math.floor(value :: number))
		refreshCash()
		if isOpen then
			renderTab()
		end
	end
end)

inventoryRemote.OnClientEvent:Connect(function(value: unknown)
	if typeof(value) == "table" then
		local next_inventory: { [string]: number } = {}
		for itemId, count in value :: { [unknown]: unknown } do
			if typeof(itemId) == "string" and typeof(count) == "number" then
				next_inventory[itemId] = count :: number
			end
		end
		inventory = next_inventory
		if isOpen then
			renderTab()
		end
	end
end)

-- Shift-click buys a stack of 5 for anything stackable; a plain click buys one.
quantityFor = function(item: ShopConfig.ItemData): number
	local stackable = item.maxOwned ~= 1
	if stackable and UserInputService:IsKeyDown(Enum.KeyCode.LeftShift) then
		return math.min(5, CONFIG.maxPurchaseQuantity)
	end
	return 1
end

-- ── First load ──────────────────────────────────────────────────────────────
task.spawn(function()
	local ok, data = pcall(function()
		return requestFunction:InvokeServer()
	end)
	if ok and typeof(data) == "table" then
		local table_ = data :: { [string]: unknown }
		if typeof(table_.cash) == "number" then
			cash = math.max(0, math.floor(table_.cash :: number))
		end
		if typeof(table_.inventory) == "table" then
			for itemId, count in table_.inventory :: { [unknown]: unknown } do
				if typeof(itemId) == "string" and typeof(count) == "number" then
					inventory[itemId] = count :: number
				end
			end
		end
	end
	refreshCash()
	if CONFIG.openOnSpawn then
		setOpen(true)
	end
end)

refreshCash()
renderTabs()
setStatus(string.format("Press %s to open the shop. Shift-click a stackable item to buy 5.",
	CONFIG.toggleKey.Name), PALETTE.textSecondary)
print("[ShopClient] ready - " .. tostring(#CONFIG.items) .. " items, toggle key " .. CONFIG.toggleKey.Name)
