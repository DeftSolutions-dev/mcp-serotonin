local CFG = {
    base_url         = "http://127.0.0.1:8765",
    poll_interval_ms = 100,
    max_depth        = 3,
    debug            = true,
    inflight_ttl_ms  = 12000,
}

local json = {}
local _esc = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b',
               ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function json_str( s )
    return '"' .. string.gsub( s, '[%z\1-\31\\"]', function( c )
        return _esc[ c ] or string.format( '\\u%04x', string.byte( c ) )
    end ) .. '"'
end

local function json_encode( v, seen )
    seen = seen or {}
    local t = type( v )
    if t == "nil"     then return "null" end
    if t == "boolean" then return v and "true" or "false" end
    if t == "number"  then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        if v == math.floor( v ) and math.abs( v ) < 1e15 then return string.format( "%d", v ) end
        return string.format( "%.17g", v )
    end
    if t == "string"  then return json_str( v ) end
    if t == "table"   then
        if seen[ v ] then return '"<circular>"' end
        seen[ v ] = true
        local n, is_arr, max_i = 0, true, 0
        for k in pairs( v ) do
            n = n + 1
            if type( k ) ~= "number" or k ~= math.floor( k ) or k < 1 then
                is_arr = false
            elseif k > max_i then max_i = k end
        end
        local out = {}
        if is_arr and n > 0 and max_i == n then
            for i = 1, n do out[ i ] = json_encode( v[ i ], seen ) end
            seen[ v ] = nil
            return "[" .. table.concat( out, "," ) .. "]"
        end
        local i = 0
        for k, val in pairs( v ) do
            i = i + 1
            out[ i ] = json_str( tostring( k ) ) .. ":" .. json_encode( val, seen )
        end
        seen[ v ] = nil
        return "{" .. table.concat( out, "," ) .. "}"
    end
    return json_str( tostring( v ) )
end

json.encode = json_encode

local function skip_ws( s, i )
    while i <= #s do
        local c = string.byte( s, i )
        if c ~= 32 and c ~= 9 and c ~= 10 and c ~= 13 then return i end
        i = i + 1
    end
    return i
end

local decode_value

local function decode_string( s, i )
    assert( string.sub( s, i, i ) == '"', "expected '\"'" )
    i = i + 1
    local out, n = {}, 0
    while i <= #s do
        local c = string.sub( s, i, i )
        if c == '"' then return table.concat( out ), i + 1 end
        if c == "\\" then
            local nxt = string.sub( s, i + 1, i + 1 )
            n = n + 1
            if     nxt == "n"  then out[ n ] = "\n"
            elseif nxt == "t"  then out[ n ] = "\t"
            elseif nxt == "r"  then out[ n ] = "\r"
            elseif nxt == "b"  then out[ n ] = "\b"
            elseif nxt == "f"  then out[ n ] = "\f"
            elseif nxt == '"'  then out[ n ] = '"'
            elseif nxt == "\\" then out[ n ] = "\\"
            elseif nxt == "/"  then out[ n ] = "/"
            elseif nxt == "u"  then
                local code = tonumber( string.sub( s, i + 2, i + 5 ), 16 ) or 0
                if code < 128 then out[ n ] = string.char( code )
                elseif code < 2048 then
                    out[ n ] = string.char( 0xc0 + math.floor( code / 64 ), 0x80 + ( code % 64 ) )
                else
                    out[ n ] = string.char( 0xe0 + math.floor( code / 4096 ),
                                            0x80 + math.floor( code / 64 ) % 64,
                                            0x80 + ( code % 64 ) )
                end
                i = i + 4
            else out[ n ] = nxt end
            i = i + 2
        else
            n = n + 1
            out[ n ] = c
            i = i + 1
        end
    end
    error( "unterminated string" )
end

local function decode_number( s, i )
    local j = i
    while j <= #s do
        local c = string.sub( s, j, j )
        if not string.find( c, "[%-0-9.eE+]" ) then break end
        j = j + 1
    end
    return tonumber( string.sub( s, i, j - 1 ) ), j
end

decode_value = function( s, i )
    i = skip_ws( s, i )
    local c = string.sub( s, i, i )
    if c == '"' then return decode_string( s, i ) end
    if c == "{" then
        local t = {}
        i = skip_ws( s, i + 1 )
        if string.sub( s, i, i ) == "}" then return t, i + 1 end
        while true do
            local k; k, i = decode_string( s, i )
            i = skip_ws( s, i )
            assert( string.sub( s, i, i ) == ":", "expected ':'" )
            local v; v, i = decode_value( s, i + 1 )
            t[ k ] = v
            i = skip_ws( s, i )
            local ch = string.sub( s, i, i )
            if     ch == "," then i = skip_ws( s, i + 1 )
            elseif ch == "}" then return t, i + 1
            else error( "expected ',' or '}'" ) end
        end
    end
    if c == "[" then
        local t, n = {}, 0
        i = skip_ws( s, i + 1 )
        if string.sub( s, i, i ) == "]" then return t, i + 1 end
        while true do
            local v; v, i = decode_value( s, i )
            n = n + 1
            t[ n ] = v
            i = skip_ws( s, i )
            local ch = string.sub( s, i, i )
            if     ch == "," then i = skip_ws( s, i + 1 )
            elseif ch == "]" then return t, i + 1
            else error( "expected ',' or ']'" ) end
        end
    end
    if c == "t" and string.sub( s, i, i + 3 ) == "true"  then return true,  i + 4 end
    if c == "f" and string.sub( s, i, i + 4 ) == "false" then return false, i + 5 end
    if c == "n" and string.sub( s, i, i + 3 ) == "null"  then return nil,   i + 4 end
    return decode_number( s, i )
end

json.decode = function( s ) local v = decode_value( s, 1 ); return v end

local handles, handle_counter = {}, 0

local function register_handle( inst )
    handle_counter = handle_counter + 1
    local h = "h" .. tostring( handle_counter )
    handles[ h ] = inst
    return h
end

local GAME_BLOCKED = {
    DataModel  = true,
    PlaceID    = true,
    GetFFlag   = true,
    SetFFlag   = true,
}

local SAFE_FIELDS_BY_CLASS = {
    _common_ = { "Name", "ClassName", "Address", },

    Part           = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance",
                       "Rotation", "LookVector", "RightVector", "UpVector", },
    MeshPart       = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance",
                       "Rotation", "LookVector", "RightVector", "UpVector",
                       "MeshId", "TextureId", "DecalTextureId", "SpecialMeshTextureId", },
    UnionOperation = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance",
                       "Rotation", "LookVector", "RightVector", "UpVector", },
    TrussPart      = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance", },
    WedgePart      = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance", },
    CornerWedgePart= { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance", },
    Seat           = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance", },
    VehicleSeat    = { "Color", "Material", "Size", "Position", "Velocity",
                       "CanCollide", "Transparency", "Reflectance", },
    SpawnLocation  = { "Color", "Material", "Size", "Position", },

    Humanoid       = { "Health", "MaxHealth", "MoveDirection", },

    Player         = { "Character", "UserId", "Team", "CameraMaxZoomDistance", },

    Bone           = { "BonePosition", },

    Sound          = { "SoundId", },

    StringValue    = { "Value", },
    NumberValue    = { "Value", },
    ObjectValue    = { "Value", },

    ProximityPrompt = { "HoldDuration", "MaxActivationDistance",
                        "ProximityActionText", "ProximityExclusivity", },

    Frame          = { "VisibleFrame", "FramePosition", "FrameBackgroundColor", "FrameBorderColor", },
    TextButton     = { "ButtonPosition", "ButtonSize", },
    ImageButton    = { "ButtonPosition", "ButtonSize", },

    Model          = {},
    Folder         = {},
    Workspace      = {},
    Camera         = {},
    Terrain        = {},
    Configuration  = {},
    Backpack       = {},

    LocalScript    = {},
    Script         = {},
    ModuleScript   = {},

    Tool           = {},

    Lighting       = {},
    Sky            = {},

    RemoteEvent       = {},
    RemoteFunction    = {},
    BindableEvent     = {},
    BindableFunction  = {},

    ScreenGui         = {},
    TextLabel         = {},
    ImageLabel        = {},

    Motor6D        = {},
    Weld           = {},
    WeldConstraint = {},
    AlignOrientation  = {},
    AlignPosition     = {},
    LinearVelocity    = {},
    Attachment        = {},
    Animator          = {},

    Decal          = {},
    Texture        = {},
    SpecialMesh    = {},
}

local function try_get( v, field )
    local ok, result = pcall( function() return v[ field ] end )
    if ok then return result end
end

local function fields_for_class( cls )
    local out = {}
    for _, f in ipairs( SAFE_FIELDS_BY_CLASS._common_ ) do out[ #out + 1 ] = f end
    local specific = cls and SAFE_FIELDS_BY_CLASS[ cls ]
    if specific then
        for _, f in ipairs( specific ) do out[ #out + 1 ] = f end
    end
    return out
end

local function looks_like_vector3( v )
    local x = try_get( v, "X" )
    local y = try_get( v, "Y" )
    local z = try_get( v, "Z" )
    return type( x ) == "number" and type( y ) == "number" and type( z ) == "number"
end

local function looks_like_color3( v )
    local r = try_get( v, "R" )
    local g = try_get( v, "G" )
    local b = try_get( v, "B" )
    return type( r ) == "number" and type( g ) == "number" and type( b ) == "number"
end

local function looks_like_instance( v )
    local cls = try_get( v, "ClassName" )
    if type( cls ) ~= "string" or cls == "" then return false, nil end
    local gc = try_get( v, "GetChildren" )
    if type( gc ) ~= "function" then return false, cls end
    return true, cls
end

local serialize

local function serialize_value( v, depth, seen )
    local t = type( v )
    if t == "nil" or t == "boolean" or t == "number" or t == "string" then return v end
    if t == "function" or t == "thread" or t == "cdata" then return "<" .. t .. ">" end

    if t == "userdata" then
        local is_inst, cls = looks_like_instance( v )
        if is_inst then
            return {
                __type    = "Instance",
                handle    = register_handle( v ),
                ClassName = cls,
                Name      = try_get( v, "Name" ) or "",
            }
        end
        if looks_like_vector3( v ) then
            return { __type = "Vector3", X = v.X, Y = v.Y, Z = v.Z }
        end
        if looks_like_color3( v ) then
            return { __type = "Color3", R = v.R, G = v.G, B = v.B }
        end
        return tostring( v )
    end

    if t == "table" then
        return serialize( v, depth + 1, seen )
    end

    return tostring( v )
end

local function read_instance_safe( inst, depth, seen )
    local is_inst, cls = looks_like_instance( inst )
    if not is_inst then return serialize_value( inst, depth, seen ) end

    local node = {
        __type    = "Instance",
        handle    = register_handle( inst ),
        ClassName = cls,
        Name      = try_get( inst, "Name" ) or "",
    }
    local addr = try_get( inst, "Address" )
    if addr ~= nil then node.Address = addr end

    local parent = try_get( inst, "Parent" )
    if parent ~= nil then
        local pn = try_get( parent, "Name" )
        if pn ~= nil then node.ParentName = pn end
    end

    for _, f in ipairs( fields_for_class( cls ) ) do
        if f ~= "Name" and f ~= "ClassName" and f ~= "Address" then
            local val = try_get( inst, f )
            if val ~= nil then
                node[ f ] = serialize_value( val, ( depth or 0 ) + 1, seen )
            end
        end
    end
    return node
end

serialize = function( v, depth, seen )
    depth = depth or 0
    seen  = seen  or {}
    local t = type( v )
    if t == "nil"     then return nil end
    if t == "boolean" or t == "number" or t == "string" then return v end
    if t == "function" or t == "thread" or t == "cdata" then return "<" .. t .. ">" end
    if depth >= CFG.max_depth then return "<max_depth>" end

    if t == "userdata" then
        local is_inst = looks_like_instance( v )
        if is_inst then return read_instance_safe( v, depth, seen ) end
        if looks_like_vector3( v ) then return { __type = "Vector3", X = v.X, Y = v.Y, Z = v.Z } end
        if looks_like_color3( v )  then return { __type = "Color3",  R = v.R, G = v.G, B = v.B } end
        return tostring( v )
    end

    if t == "table" then
        if seen[ v ] then return "<circular>" end
        seen[ v ] = true
        if looks_like_vector3( v ) then
            seen[ v ] = nil
            return { __type = "Vector3", X = v.X, Y = v.Y, Z = v.Z }
        end
        local out, count, is_arr = {}, 0, true
        for k in pairs( v ) do
            if type( k ) ~= "number" then is_arr = false; break end
        end
        if is_arr then
            for i, val in ipairs( v ) do
                count = count + 1
                if count > 500 then out[ count ] = "<truncated>"; break end
                out[ i ] = serialize( val, depth + 1, seen )
            end
        else
            for k, val in pairs( v ) do
                count = count + 1
                if count > 200 then out.__truncated = true; break end
                out[ tostring( k ) ] = serialize( val, depth + 1, seen )
            end
        end
        seen[ v ] = nil
        return out
    end

    return tostring( v )
end

local function resolve_target( target )
    if target == nil then return nil, "nil target" end
    if type( target ) ~= "string" then return nil, "target must be string" end
    if handles[ target ] then return handles[ target ] end

    if string.find( target, "^game%." ) then
        for blocked, _ in pairs( GAME_BLOCKED ) do
            if target == "game." .. blocked or string.find( target, "^game%." .. blocked .. "%." ) then
                return nil, "blocked unsafe path: " .. target
            end
        end
    end

    local env = getfenv( 1 )
    local cur = env
    local is_env = true
    for part in string.gmatch( target, "[^%.]+" ) do
        if cur == nil then return nil, "not found (nil at '" .. part .. "'): " .. target end

        local nxt
        if is_env then
            nxt = env[ part ]
            is_env = false
        else
            if cur == env[ "game" ] and GAME_BLOCKED[ part ] then
                return nil, "blocked unsafe path: " .. target
            end
            local ok, v = pcall( function() return cur[ part ] end )
            if ok then nxt = v end
            if nxt == nil then
                local ok2, child = pcall( function() return cur:FindFirstChild( part ) end )
                if ok2 and child ~= nil then nxt = child end
            end
        end

        if nxt == nil then return nil, "not found: '" .. part .. "' in " .. target end
        cur = nxt
    end
    return cur
end

local function op_ping() return "pong" end

local function op_eval( args )
    local code = args.code
    if type( code ) ~= "string" then return nil, "missing 'code'" end
    local chunk, err = loadstring( "return (function()\n" .. code .. "\nend)()" )
    if not chunk then
        chunk, err = loadstring( code )
    end
    if not chunk then return nil, "parse: " .. tostring( err ) end
    local ok, result = pcall( chunk )
    if not ok then return nil, "runtime: " .. tostring( result ) end
    return serialize( result, 0 )
end

local function op_safe_inspect( args )
    local inst, err = resolve_target( args.target )
    if not inst then return nil, err end

    local node = read_instance_safe( inst, 0, {} )

    local get_attrs = try_get( inst, "GetAttributes" )
    if type( get_attrs ) == "function" then
        local ok, list = pcall( function() return inst:GetAttributes() end )
        if ok and type( list ) == "table" then
            node.Attributes = serialize( list, 0, {} )
        end
    end

    local get_ch = try_get( inst, "GetChildren" )
    if type( get_ch ) == "function" then
        local ok, ch = pcall( function() return inst:GetChildren() end )
        if ok and type( ch ) == "table" then
            local lim = args.max_children or 100
            local children = {}
            for i = 1, math.min( #ch, lim ) do
                local c = ch[ i ]
                children[ i ] = {
                    __type    = "Instance",
                    handle    = register_handle( c ),
                    ClassName = try_get( c, "ClassName" ) or "?",
                    Name      = try_get( c, "Name" ) or "",
                }
            end
            node.Children     = children
            node.ChildrenCount = #ch
        end
    end
    return node
end

local function op_inspect( args )
    return op_safe_inspect( args )
end

local function op_snapshot( args )
    local out = { __type = "Snapshot", Errors = {} }
    local function section( name, fn )
        local ok, err = pcall( fn )
        if not ok then out.Errors[ name ] = tostring( err ) end
    end

    section( "window", function()
        local ok, w, h = pcall( cheat.getWindowSize )
        if ok then out.WindowSize = { W = w, H = h } end
    end )

    section( "input", function()
        local ok1, mp = pcall( utility.GetMousePos );  if ok1 then out.MousePos = serialize_value( mp, 0, {} ) end
        local ok2, dt = pcall( utility.GetDeltaTime ); if ok2 then out.DeltaTime = dt end
        local ok3, tc = pcall( utility.GetTickCount ); if ok3 then out.TickCount = tc end
        local ok4, ms = pcall( utility.GetMenuState ); if ok4 then out.MenuOpen = ms end
    end )

    section( "workspace", function()
        local ws = game.Workspace
        if not ws then return end
        local ch = ws:GetChildren()
        out.Workspace = { ChildrenCount = #ch, Children = {} }
        for i = 1, math.min( #ch, 40 ) do
            local c   = ch[ i ]
            local cls = try_get( c, "ClassName" )
            local rec = { Name = try_get( c, "Name" ), ClassName = cls }
            if cls == "Folder" or cls == "Model" then
                local ok2, cch = pcall( function() return c:GetChildren() end )
                if ok2 and type( cch ) == "table" then rec.ChildrenCount = #cch end
            end
            out.Workspace.Children[ i ] = rec
        end
    end )

    section( "players", function()
        local p = game.Players
        if not p then return end
        local ch = p:GetChildren()
        local list = {}
        for i = 1, math.min( #ch, 60 ) do
            list[ i ] = {
                Name        = try_get( ch[ i ], "Name" ),
                UserId      = try_get( ch[ i ], "UserId" ),
                DisplayName = try_get( ch[ i ], "DisplayName" ),
            }
        end
        out.Players       = list
        out.PlayersCount  = #ch
    end )

    section( "entity", function()
        if not entity or type( entity.GetPlayers ) ~= "function" then return end
        local players = entity.GetPlayers()
        if type( players ) ~= "table" then return end
        local rows = {}
        for i, ep in ipairs( players ) do
            local rec = {}
            for _, f in ipairs( { "Name", "Health", "MaxHealth", "IsAlive",
                                  "IsEnemy", "IsVisible", "Weapon" } ) do
                local okv, v = pcall( function() return ep[ f ] end )
                if okv and v ~= nil then rec[ f ] = v end
            end
            local okh, hrp = pcall( function() return ep:GetBonePosition( "HumanoidRootPart" ) end )
            if okh and hrp ~= nil then
                rec.HRP = { X = hrp.X, Y = hrp.Y, Z = hrp.Z }
            end
            rows[ i ] = rec
        end
        out.EntityPlayers      = rows
        out.EntityPlayersCount = #players
    end )

    return out
end

local function op_dive( args )
    local root, err = resolve_target( args.root or "game.Workspace" )
    if not root then return nil, err end
    local max_children = args.max_children or 100
    local max_depth    = math.min( args.max_depth or 2, 4 )

    local function walk( inst, depth )
        local node = read_instance_safe( inst, 0, {} )
        if depth <= 0 then return node end
        local ok, ch = pcall( function() return inst:GetChildren() end )
        if not ok or type( ch ) ~= "table" then return node end
        local kids = {}
        local lim = math.min( #ch, max_children )
        for i = 1, lim do kids[ i ] = walk( ch[ i ], depth - 1 ) end
        if #ch > lim then node.Truncated = #ch - lim end
        node.Children     = kids
        node.ChildrenCount = #ch
        return node
    end

    return walk( root, max_depth )
end

local function op_live_dump( args )
    local include_bones = args.include_bones ~= false
    local out = {}

    local envgame = getfenv( 1 ).game
    local live = envgame and envgame.Workspace and envgame.Workspace:FindFirstChild( "Live" )

    if entity and type( entity.GetPlayers ) == "function" then
        local ok, players = pcall( entity.GetPlayers )
        if ok and type( players ) == "table" then
            for i, p in ipairs( players ) do
                local rec = {}
                for _, f in ipairs( { "Name", "Health", "MaxHealth", "IsAlive",
                                      "IsEnemy", "IsVisible", "UserId", "Team",
                                      "Weapon", "IsWhitelisted", "DisplayName",
                                      "TeamColor" } ) do
                    local okv, v = pcall( function() return p[ f ] end )
                    if okv and v ~= nil then rec[ f ] = v end
                end
                if include_bones then
                    for _, b in ipairs( { "HumanoidRootPart", "Head", "UpperTorso" } ) do
                        local okb, bp = pcall( function() return p:GetBonePosition( b ) end )
                        if okb and bp ~= nil then rec[ "Bone_" .. b ] = serialize_value( bp, 0, {} ) end
                    end
                end

                local hrp = rec.Bone_HumanoidRootPart
                if hrp and utility and utility.WorldToScreen then
                    local ok2, sx, sy, onscr = pcall( utility.WorldToScreen,
                        Vector3.new( hrp.X, hrp.Y, hrp.Z ) )
                    if ok2 then rec.Screen = { X = sx, Y = sy, OnScreen = onscr } end
                end

                if live then
                    local name = rec.Name
                    local model = name and live:FindFirstChild( name )
                    local tank  = model and model:FindFirstChild( "Tank" )
                    if tank then
                        local okc, ch = pcall( function() return tank:GetChildren() end )
                        if okc and type( ch ) == "table" then
                            rec.TankPartsCount = #ch
                            local classes = {}
                            for _, c in ipairs( ch ) do
                                local cls = try_get( c, "ClassName" ) or "?"
                                classes[ cls ] = ( classes[ cls ] or 0 ) + 1
                            end
                            rec.TankPartClasses = classes
                        end
                    end
                end
                out[ i ] = rec
            end
        end
    end
    return out
end

local function op_class_counts( args )
    local root, err = resolve_target( args.root or "game.Workspace" )
    if not root then return nil, err end
    local limit = args.limit or 3000
    local counts, total = {}, 0
    local ok, desc = pcall( function() return root:GetDescendants() end )
    if not ok or type( desc ) ~= "table" then return { Total = 0, Classes = {} } end
    for i = 1, math.min( #desc, limit ) do
        total = total + 1
        local cls = try_get( desc[ i ], "ClassName" )
        if cls then counts[ cls ] = ( counts[ cls ] or 0 ) + 1 end
    end
    local sorted = {}
    for c, n in pairs( counts ) do sorted[ #sorted + 1 ] = { ClassName = c, Count = n } end
    table.sort( sorted, function( a, b ) return a.Count > b.Count end )
    return {
        Total     = total,
        Truncated = ( #desc > limit ) and ( #desc - limit ) or 0,
        Classes   = sorted,
    }
end

local function op_list_scripts( args )
    local root, err = resolve_target( args.root or "game.Workspace" )
    if not root then return nil, err end
    local limit = args.limit or 500
    local out = {}
    local ok, desc = pcall( function() return root:GetDescendants() end )
    if not ok or type( desc ) ~= "table" then return out end
    for i = 1, #desc do
        if #out >= limit then break end
        local cls = try_get( desc[ i ], "ClassName" )
        if cls == "Script" or cls == "LocalScript" or cls == "ModuleScript" then

            local path, cur, depth = {}, desc[ i ], 0
            while cur and depth < 12 do
                local n = try_get( cur, "Name" )
                if type( n ) == "string" and n ~= "" then path[ #path + 1 ] = n end
                local p = try_get( cur, "Parent" )
                if p == nil then break end
                cur = p
                depth = depth + 1
            end
            local rev = {}
            for j = #path, 1, -1 do rev[ #rev + 1 ] = path[ j ] end
            out[ #out + 1 ] = {
                Name      = try_get( desc[ i ], "Name" ),
                ClassName = cls,
                Path      = table.concat( rev, "." ),
            }
        end
    end
    return out
end

local function op_search( args )
    local root, err = resolve_target( args.root or "game.Workspace" )
    if not root then return nil, err end
    local pat   = string.lower( args.pattern or "" )
    local cls_f = args.class_name
    local max_r = args.max_results or 100
    local out   = {}
    local ok, desc = pcall( function() return root:GetDescendants() end )
    if not ok or type( desc ) ~= "table" then return out end
    for i = 1, #desc do
        if #out >= max_r then break end
        local inst = desc[ i ]
        local nm   = try_get( inst, "Name" )
        if type( nm ) == "string" and ( pat == "" or string.find( string.lower( nm ), pat, 1, true ) ) then
            local cls = try_get( inst, "ClassName" )
            if not cls_f or cls == cls_f then
                out[ #out + 1 ] = {
                    __type    = "Instance",
                    handle    = register_handle( inst ),
                    ClassName = cls,
                    Name      = nm,
                }
            end
        end
    end
    return out
end

local OPS = {
    ping          = op_ping,
    eval          = op_eval,
    inspect       = op_inspect,
    safe_inspect  = op_safe_inspect,
    snapshot      = op_snapshot,
    dive          = op_dive,
    live_dump     = op_live_dump,
    class_counts  = op_class_counts,
    list_scripts  = op_list_scripts,
    search        = op_search,
}

local function dispatch( op, args )
    local fn = OPS[ op ]
    if not fn then return nil, "unknown op: " .. tostring( op ) end
    local ok, result, err = pcall( fn, args or {} )
    if not ok then return nil, "handler crash: " .. tostring( result ) end
    return result, err
end

function _sero_find_class( cls, root, limit )
    root  = root  or game.Workspace
    limit = limit or 200
    local out = {}
    local ok, desc = pcall( function() return root:GetDescendants() end )
    if not ok or type( desc ) ~= "table" then return out end
    for i = 1, #desc do
        if #out >= limit then break end
        local c = try_get( desc[ i ], "ClassName" )
        if c == cls then out[ #out + 1 ] = desc[ i ] end
    end
    return out
end

function _sero_find_player( name )
    local live = game.Workspace:FindFirstChild( "Live" )
    if not live then return nil end
    return live:FindFirstChild( name )
end

function _sero_my_pos()
    local lp = entity.GetLocalPlayer()
    if not lp then return nil end
    local ok, v = pcall( function() return lp:GetBonePosition( "HumanoidRootPart" ) end )
    if ok then return v end
end

local in_flight    = false
local inflight_at  = 0
local last_tick    = 0
local poll_count   = 0
local reply_count  = 0
local JSON_HEADERS = { [ "Content-Type" ] = "application/json",
                       [ "Accept" ]       = "application/json" }

local function send_result( cmd_id, result, err )
    local body = json.encode( { id = cmd_id, result = result, error = err } )
    if CFG.debug then print( "[bridge] POST /result id=" .. tostring( cmd_id ) .. " bytes=" .. #body ) end
    http.Post( CFG.base_url .. "/result", JSON_HEADERS, body, function( resp )
        if CFG.debug then print( "[bridge] /result reply: " .. tostring( resp and #resp or "nil" ) ) end
    end )
end

local function process_batch( response )
    if type( response ) ~= "string" or response == "" then return end
    local ok, cmds = pcall( json.decode, response )
    if not ok or type( cmds ) ~= "table" then
        if CFG.debug then print( "[bridge] bad response: " .. tostring( cmds ):sub( 1, 80 ) ) end
        return
    end
    if #cmds > 0 and CFG.debug then print( "[bridge] got " .. #cmds .. " cmd(s)" ) end
    for _, cmd in ipairs( cmds ) do
        if type( cmd ) == "table" and cmd.id and cmd.op then
            if CFG.debug then print( "[bridge] dispatch op=" .. cmd.op .. " id=" .. cmd.id ) end
            local result, err = dispatch( cmd.op, cmd.args )
            send_result( cmd.id, result, err )
        end
    end
end

local function poll()
    local now = utility.GetTickCount()
    if in_flight then
        if now - inflight_at > CFG.inflight_ttl_ms then
            if CFG.debug then print( "[bridge] watchdog: reset in_flight after " ..
                ( now - inflight_at ) .. "ms" ) end
            in_flight = false
        else
            return
        end
    end
    in_flight   = true
    inflight_at = now
    poll_count  = poll_count + 1
    if CFG.debug and poll_count % 50 == 1 then
        print( "[bridge] poll #" .. poll_count .. " replies=" .. reply_count )
    end
    http.Get( CFG.base_url .. "/poll", JSON_HEADERS, function( response )
        reply_count = reply_count + 1
        in_flight   = false
        local ok, perr = pcall( process_batch, response )
        if not ok then print( "[bridge] process error: " .. tostring( perr ) ) end
    end )
end

cheat.register( "onUpdate", function()
    local now = utility.GetTickCount()
    if now - last_tick >= CFG.poll_interval_ms then
        last_tick = now
        poll()
    end
end )

cheat.register( "shutdown", function()
    handles        = {}
    handle_counter = 0
end )

print( "[serotonin-bridge v2] loaded, polling " .. CFG.base_url .. " every " .. CFG.poll_interval_ms .. " ms" )
print( "[serotonin-bridge v2] ops: ping eval inspect safe_inspect snapshot dive live_dump class_counts list_scripts search" )
