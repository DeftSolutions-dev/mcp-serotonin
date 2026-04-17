local CFG = {
    base_url         = "http://127.0.0.1:8765",
    poll_interval_ms = 100,
    max_depth        = 4,
    debug            = false,
    inflight_ttl_ms  = 15000,
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

local function resolve_target( target )
    if target == nil then return nil, "nil target" end
    if type( target ) ~= "string" then return nil, "target must be string" end
    if handles[ target ] then return handles[ target ] end

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

local function try_get( v, field )
    local ok, result = pcall( function() return v[ field ] end )
    if ok then return result end
end

local function looks_like_instance( v )
    local cls = try_get( v, "ClassName" )
    if type( cls ) ~= "string" or cls == "" then return false end
    local gc = try_get( v, "GetChildren" )
    return type( gc ) == "function"
end

local function looks_like_vector3( v )
    local x, y, z = try_get( v, "X" ), try_get( v, "Y" ), try_get( v, "Z" )
    return type( x ) == "number" and type( y ) == "number" and type( z ) == "number"
end

local function looks_like_color3( v )
    local r, g, b = try_get( v, "R" ), try_get( v, "G" ), try_get( v, "B" )
    return type( r ) == "number" and type( g ) == "number" and type( b ) == "number"
end

local function serialize( v, depth, maxdepth )
    depth    = depth    or 0
    maxdepth = maxdepth or CFG.max_depth
    local t = type( v )
    if t == "nil"     then return nil end
    if t == "boolean" or t == "number" or t == "string" then return v end
    if t == "function" or t == "thread" or t == "cdata" then return "<" .. t .. ">" end
    if depth >= maxdepth then return "<max_depth>" end

    if looks_like_instance( v ) then
        local parent_name
        local parent = try_get( v, "Parent" )
        if parent ~= nil then parent_name = try_get( parent, "Name" ) end
        return {
            __type    = "Instance",
            handle    = register_handle( v ),
            ClassName = try_get( v, "ClassName" ),
            Name      = try_get( v, "Name" ) or "",
            Parent    = parent_name,
            Address   = try_get( v, "Address" ),
        }
    end

    if t == "userdata" then
        if looks_like_vector3( v ) then
            return { __type = "Vector3", X = v.X, Y = v.Y, Z = v.Z }
        end
        if looks_like_color3( v ) then
            return { __type = "Color3", R = v.R, G = v.G, B = v.B }
        end
        return tostring( v )
    end

    if t == "table" then
        if looks_like_vector3( v ) then
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
                out[ i ] = serialize( val, depth + 1, maxdepth )
            end
        else
            for k, val in pairs( v ) do
                count = count + 1
                if count > 200 then out.__truncated = true; break end
                out[ tostring( k ) ] = serialize( val, depth + 1, maxdepth )
            end
        end
        return out
    end

    return tostring( v )
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
    return serialize( result, 0, args.maxdepth or CFG.max_depth )
end

local KNOWN_PROPS = {
    "Name", "ClassName", "Address", "Position", "Size", "Color", "Material",
    "Transparency", "Reflectance", "Velocity", "Rotation", "LookVector",
    "RightVector", "UpVector", "CanCollide", "Health", "MaxHealth",
    "MoveDirection", "UserId", "Team", "DisplayName", "Value",
    "HoldDuration", "MaxActivationDistance", "ProximityActionText",
    "CameraMaxZoomDistance", "SoundId", "MeshId", "TextureId", "BonePosition",
}

local function op_inspect( args )
    local inst, err = resolve_target( args.target )
    if not inst then return nil, err end

    local props = { __handle = register_handle( inst ) }

    local parent_name
    local parent = try_get( inst, "Parent" )
    if parent ~= nil then parent_name = try_get( parent, "Name" ) end
    props.ParentName = parent_name

    for _, k in ipairs( KNOWN_PROPS ) do
        local val = try_get( inst, k )
        if val ~= nil then props[ k ] = serialize( val, 0, 2 ) end
    end

    local attrs = try_get( inst, "GetAttributes" )
    if type( attrs ) == "function" then
        local ok, list = pcall( function() return inst:GetAttributes() end )
        if ok and type( list ) == "table" then props.Attributes = serialize( list, 0, 2 ) end
    end

    local get_children = try_get( inst, "GetChildren" )
    if type( get_children ) == "function" then
        local ok, ch = pcall( function() return inst:GetChildren() end )
        if ok and type( ch ) == "table" then
            local children = {}
            for i, c in ipairs( ch ) do
                if i > ( args.max_children or 100 ) then break end
                children[ i ] = serialize( c, 0, 1 )
            end
            props.Children     = children
            props.ChildrenCount = #ch
        end
    end

    return props
end

local OPS = {
    ping    = op_ping,
    eval    = op_eval,
    inspect = op_inspect,
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
    local desc = root:GetDescendants()
    for i = 1, #desc do
        if #out >= limit then break end
        local inst = desc[ i ]
        local ok, c = pcall( function() return inst.ClassName end )
        if ok and c == cls then out[ #out + 1 ] = inst end
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

function _sero_nearest( cls, origin, radius, root )
    origin = origin or _sero_my_pos()
    if not origin then return nil, "no origin" end
    root   = root or game.Workspace
    radius = radius or math.huge
    local best_d, best_i = radius + 1, nil
    for _, inst in ipairs( root:GetDescendants() ) do
        local ok_c, c = pcall( function() return inst.ClassName end )
        if ok_c and ( cls == nil or c == cls ) then
            local ok_p, p = pcall( function() return inst.Position end )
            if ok_p and type( p ) == "userdata" then
                local dx = p.X - origin.X
                local dy = p.Y - origin.Y
                local dz = p.Z - origin.Z
                local d  = math.sqrt( dx * dx + dy * dy + dz * dz )
                if d < best_d then best_d, best_i = d, inst end
            end
        end
    end
    if best_i then return { instance = best_i, distance = best_d } end
    return nil
end

function _sero_stats( root, top_n )
    root  = root  or game.Workspace
    top_n = top_n or 20
    local counts, total = {}, 0
    for _, inst in ipairs( root:GetDescendants() ) do
        total = total + 1
        local ok, c = pcall( function() return inst.ClassName end )
        if ok and c then counts[ c ] = ( counts[ c ] or 0 ) + 1 end
    end
    local sorted = {}
    for c, n in pairs( counts ) do sorted[ #sorted + 1 ] = { ClassName = c, Count = n } end
    table.sort( sorted, function( a, b ) return a.Count > b.Count end )
    local top = {}
    for i = 1, math.min( top_n, #sorted ) do top[ i ] = sorted[ i ] end
    return { Total = total, Top = top }
end

function _sero_scripts( root, limit )
    root  = root  or game
    limit = limit or 500
    local out = {}
    for _, inst in ipairs( root:GetDescendants() ) do
        if #out >= limit then break end
        local ok, c = pcall( function() return inst.ClassName end )
        if ok and ( c == "Script" or c == "LocalScript" or c == "ModuleScript" ) then
            local path, cur, depth = {}, inst, 0
            while cur and depth < 12 do
                local ok_n, n = pcall( function() return cur.Name end )
                if ok_n and type( n ) == "string" and n ~= "" then
                    path[ #path + 1 ] = n
                end
                local ok_p, p = pcall( function() return cur.Parent end )
                if not ok_p or p == nil then break end
                cur = p
                depth = depth + 1
            end
            local rev = {}
            for i = #path, 1, -1 do rev[ #rev + 1 ] = path[ i ] end
            out[ #out + 1 ] = {
                Name      = inst.Name,
                ClassName = c,
                Path      = table.concat( rev, "." ),
            }
        end
    end
    return out
end

function _sero_project( v3 )
    local ok, sx, sy, on = pcall( utility.WorldToScreen, v3 )
    if not ok then return { x = 0, y = 0, on_screen = false, error = tostring( sx ) } end
    return { x = sx, y = sy, on_screen = on }
end

function _sero_player_snapshot( p )
    local out = {}
    for _, f in ipairs( {
        "Name", "DisplayName", "UserId", "Team", "Weapon",
        "Health", "MaxHealth", "IsAlive", "IsEnemy", "IsVisible",
        "IsWhitelisted", "Velocity", "BoundingBox", "TeamColor",
    } ) do
        local ok, v = pcall( function() return p[ f ] end )
        if ok and v ~= nil then out[ f ] = v end
    end
    local ok, hrp = pcall( function() return p:GetBonePosition( "HumanoidRootPart" ) end )
    if ok and hrp ~= nil then
        out.HRP = hrp
        local proj_ok, sx, sy, on = pcall( utility.WorldToScreen, hrp )
        if proj_ok then out.Screen = { x = sx, y = sy, on = on } end
    end
    return out
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
    http.Post( CFG.base_url .. "/result", JSON_HEADERS, body, function() end )
end

local function process_batch( response )
    if type( response ) ~= "string" or response == "" then return end
    local ok, cmds = pcall( json.decode, response )
    if not ok or type( cmds ) ~= "table" then return end
    for _, cmd in ipairs( cmds ) do
        if type( cmd ) == "table" and cmd.id and cmd.op then
            local result, err = dispatch( cmd.op, cmd.args )
            send_result( cmd.id, result, err )
        end
    end
end

local function poll()
    local now = utility.GetTickCount()
    if in_flight then
        if now - inflight_at > CFG.inflight_ttl_ms then
            in_flight = false
        else
            return
        end
    end
    in_flight   = true
    inflight_at = now
    poll_count  = poll_count + 1
    http.Get( CFG.base_url .. "/poll", JSON_HEADERS, function( response )
        reply_count = reply_count + 1
        in_flight   = false
        pcall( process_batch, response )
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

print( "[serotonin-bridge] loaded, polling " .. CFG.base_url )
