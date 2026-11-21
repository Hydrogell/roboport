// Автокомплит для RCON-консоли. Глобальные переменные: SLASH_CMDS, LUA_API.
// Подключается через <script src="..."> в console.html. Не использует ES-modules
// (CSP-friendly + кэшируется браузером один раз для всех серверов).

(function (root) {
  // ---------- Slash-команды Factorio ----------
  // Источник: lua-api.factorio.com/latest/console.html + опыт.
  // Описания на русском, signature без перевода чтобы скопировать-вставить.
  // multi: true → команда имеет под-формы (admins/banlist/permissions/etc),
  //               по выбору подставится только prefix чтобы юзер увидел
  //               подформы во второй итерации автокомплита.
  // Список выверен реальным RCON-тестом на 1.1.110 + 2.0.76 (Space Age).
  // Команды ниже либо работают через RCON, либо имеют осмысленный output.
  // Удалены: /mods, /enable-research-queue, /reset-tips-and-tricks,
  // /toggle-rail-block-visualisation (Unknown command в RCON), /cheat all,
  // /color, /screenshot, /perf-avg-frames (бесполезны на dedicated server).
  // /permissions и /config открывают in-game GUI — в RCON просто печатают
  // description, но юзеру это знать полезно (помечено в desc).
  const SLASH_CMDS = [
    // === Информация ===
    { sig: '/help',          desc: window.rpT('rcon.command.help') },
    { sig: '/admins',        desc: window.rpT('rcon.command.admins') },
    { sig: '/version',       desc: window.rpT('rcon.command.version') },
    { sig: '/seed',          desc: window.rpT('rcon.command.seed') },
    { sig: '/time',          desc: window.rpT('rcon.command.time') },
    { sig: '/evolution',     desc: window.rpT('rcon.command.evolution') },

    // === Игроки ===
    { sig: '/players',          desc: window.rpT('rcon.command.players') },
    { sig: '/players online',   desc: window.rpT('rcon.command.players_online') },
    { sig: '/players count',    desc: window.rpT('rcon.command.players_count') },
    { sig: '/promote ',         desc: window.rpT('rcon.command.promote'), cursor: -1 },
    { sig: '/demote ',          desc: window.rpT('rcon.command.demote'), cursor: -1 },

    // === Чат / коммуникация ===
    { sig: '/shout ',           desc: window.rpT('rcon.command.shout'), cursor: -1 },
    { sig: '/whisper ',         desc: window.rpT('rcon.command.whisper'), cursor: -1 },
    { sig: '/clear',            desc: window.rpT('rcon.command.clear') },
    { sig: '/purge ',           desc: window.rpT('rcon.command.purge'), cursor: -1 },
    { sig: '/mute-programmable-speaker',  desc: window.rpT('rcon.command.mute_programmable_speaker') },
    { sig: '/mute-spectator',   desc: window.rpT('rcon.command.mute_spectator') },
    { sig: '/ignore ',          desc: window.rpT('rcon.command.ignore'), cursor: -1 },
    { sig: '/unignore ',        desc: window.rpT('rcon.command.unignore'), cursor: -1 },
    { sig: '/mute ',            desc: window.rpT('rcon.command.mute'), cursor: -1 },
    { sig: '/unmute ',          desc: window.rpT('rcon.command.unmute'), cursor: -1 },

    // === Бан / whitelist ===
    { sig: '/ban ',             desc: window.rpT('rcon.command.ban'), cursor: -1 },
    { sig: '/unban ',           desc: window.rpT('rcon.command.unban'), cursor: -1 },
    { sig: '/banlist add ',     desc: window.rpT('rcon.command.banlist_add'), cursor: -1 },
    { sig: '/banlist remove ',  desc: window.rpT('rcon.command.banlist_remove'), cursor: -1 },
    { sig: '/banlist get',      desc: window.rpT('rcon.command.banlist_get') },
    { sig: '/banlist clear',    desc: window.rpT('rcon.command.banlist_clear') },
    { sig: '/whitelist add ',   desc: window.rpT('rcon.command.whitelist_add'), cursor: -1 },
    { sig: '/whitelist remove ', desc: window.rpT('rcon.command.whitelist_remove'), cursor: -1 },
    { sig: '/whitelist get',    desc: window.rpT('rcon.command.whitelist_get') },
    { sig: '/whitelist clear',  desc: window.rpT('rcon.command.whitelist_clear') },
    { sig: '/kick ',            desc: window.rpT('rcon.command.kick'), cursor: -1 },

    // === Сейв / окружение ===
    { sig: '/server-save',      desc: window.rpT('rcon.command.server_save') },
    { sig: '/save ',            desc: window.rpT('rcon.command.save'), cursor: -1 },

    // === Debug ===
    { sig: '/toggle-action-logging',  desc: window.rpT('rcon.command.toggle_action_logging') },
    { sig: '/toggle-heavy-mode',      desc: window.rpT('rcon.command.toggle_heavy_mode') },

    // === Lua ===
    { sig: '/c ',               desc: window.rpT('rcon.command.c'), cursor: -1 },
    { sig: '/silent-command ',  desc: window.rpT('rcon.command.silent_command'), cursor: -1 },
    { sig: '/measured-command ', desc: window.rpT('rcon.command.measured_command'), cursor: -1 },
    { sig: '/command ',         desc: window.rpT('rcon.command.command'), cursor: -1 },

    // === Permissions / config / прочее ===
    { sig: '/permissions',      desc: window.rpT('rcon.command.permissions') },
    { sig: '/config',           desc: window.rpT('rcon.command.config') },
    { sig: '/swap-players ',    desc: window.rpT('rcon.command.swap_players'), cursor: -1 },
    { sig: '/open ',            desc: window.rpT('rcon.command.open'), cursor: -1 },

    // === Алерты ===
    { sig: '/alerts mute-all',  desc: window.rpT('rcon.command.alerts_mute_all') },
    { sig: '/alerts unmute-all', desc: window.rpT('rcon.command.alerts_unmute_all') },
  ];

  // ---------- Lua API (для /c <here>) ----------
  // Top-level globals доступные в RCON-Lua. Не полный API, фокус на типичных
  // юзкейсах админа: чит-предметы, evolution, тех, телепорт, статы.
  const LUA_TOP = [
    { name: 'game', desc: window.rpT('rcon.command.game') },
    { name: 'script', desc: 'LuaBootstrap — script.active_mods, script.on_event' },
    { name: 'remote', desc: window.rpT('rcon.command.remote') },
    { name: 'defines', desc: window.rpT('rcon.command.defines') },
    { name: 'settings', desc: window.rpT('rcon.command.settings') },
    { name: 'log', desc: window.rpT('rcon.command.log') },
    { name: 'serpent', desc: window.rpT('rcon.command.serpent') },
    { name: 'rcon', desc: window.rpT('rcon.command.rcon') },
    { name: 'rendering', desc: window.rpT('rcon.command.rendering') },
    { name: 'helpers', desc: 'helpers.json_to_table, helpers.write_file (2.0+)' },
  ];

  // Members часто используемых классов. Маркер цели в комментарии — что чем
  // вернётся (LuaPlayer, LuaForce, table, etc), для будущей doc-помощи.
  const LUA_MEMBERS = {
    game: [
      { name: 'print', sig: 'print(msg, [color])', desc: window.rpT('rcon.command.print') },
      { name: 'players', desc: window.rpT('rcon.command.players_label') },
      { name: 'surfaces', desc: window.rpT('rcon.command.surfaces') },
      { name: 'forces', desc: window.rpT('rcon.command.forces') },
      { name: 'connected_players', desc: window.rpT('rcon.command.connected_players') },
      { name: 'tick', desc: window.rpT('rcon.command.tick') },
      { name: 'speed', desc: window.rpT('rcon.command.speed') },
      { name: 'difficulty_settings', desc: window.rpT('rcon.command.difficulty_settings') },
      { name: 'map_settings', desc: window.rpT('rcon.command.map_settings') },
      { name: 'pollution_statistics', desc: window.rpT('rcon.command.pollution_statistics') },
      { name: 'create_random_generator', sig: 'create_random_generator([seed])' },
      { name: 'set_game_state', sig: 'set_game_state{game_finished=true, ...}' },
      { name: 'take_screenshot', sig: 'take_screenshot{...}' },
      { name: 'reload_script', desc: window.rpT('rcon.command.reload_script') },
      { name: 'reload_mods', desc: window.rpT('rcon.command.reload_mods') },
      { name: 'auto_save', sig: 'auto_save([name])', desc: window.rpT('rcon.command.auto_save') },
      { name: 'server_save', sig: 'server_save([name])' },
      { name: 'finished', desc: window.rpT('rcon.command.finished') },
      { name: 'finished_but_continuing', desc: window.rpT('rcon.command.finished_but_continuing') },
      { name: 'evaluate_expression', sig: 'evaluate_expression(expr)' },
    ],
    'game.player': [
      { name: 'print', sig: 'print(msg, [color])' },
      { name: 'character', desc: window.rpT('rcon.game_player.character') },
      { name: 'force', desc: window.rpT('rcon.game_player.force') },
      { name: 'position', desc: window.rpT('rcon.game_player.position') },
      { name: 'surface', desc: window.rpT('rcon.game_player.surface') },
      { name: 'name', desc: window.rpT('rcon.game_player.name') },
      { name: 'index', desc: window.rpT('rcon.game_player.index') },
      { name: 'admin', desc: window.rpT('rcon.game_player.admin') },
      { name: 'connected', desc: window.rpT('rcon.game_player.connected') },
      { name: 'cheat_mode', desc: window.rpT('rcon.game_player.cheat_mode') },
      { name: 'controller_type', desc: 'defines.controllers.character/god/spectator/...' },
      { name: 'gui', desc: window.rpT('rcon.game_player.gui') },
      { name: 'opened', desc: window.rpT('rcon.game_player.opened') },
      { name: 'selected', desc: window.rpT('rcon.game_player.selected') },
      { name: 'teleport', sig: 'teleport(position, [surface])' },
      { name: 'insert', sig: 'insert{name=, count=}' },
      { name: 'remove_item', sig: 'remove_item{name=, count=}' },
      { name: 'clear_items_inside', desc: window.rpT('rcon.game_player.clear_items_inside') },
      { name: 'get_inventory', sig: 'get_inventory(defines.inventory.character_main)' },
      { name: 'kick', sig: 'kick([reason])' },
      { name: 'ban', sig: 'ban([reason])' },
      { name: 'set_controller', sig: 'set_controller{type=defines.controllers.god}' },
      { name: 'request_translation', sig: 'request_translation(localised_string)' },
    ],
    'game.force': [
      { name: 'name', desc: window.rpT('rcon.game_force.name') },
      { name: 'players', desc: window.rpT('rcon.game_force.players') },
      { name: 'technologies', desc: window.rpT('rcon.game_force.technologies') },
      { name: 'recipes', desc: window.rpT('rcon.game_force.recipes') },
      { name: 'research_progress', desc: window.rpT('rcon.game_force.research_progress') },
      { name: 'current_research', desc: window.rpT('rcon.game_force.current_research') },
      { name: 'add_research', sig: 'add_research(tech)', desc: window.rpT('rcon.game_force.add_research') },
      { name: 'enable_all_recipes', desc: window.rpT('rcon.game_force.enable_all_recipes') },
      { name: 'enable_all_technologies', desc: window.rpT('rcon.game_force.enable_all_technologies') },
      { name: 'research_all_technologies', sig: 'research_all_technologies([include_disabled])' },
      { name: 'reset_technologies', desc: window.rpT('rcon.game_force.reset_technologies') },
      { name: 'chart_all', sig: 'chart_all([surface])', desc: window.rpT('rcon.game_force.chart_all') },
      { name: 'rechart', desc: window.rpT('rcon.game_force.rechart') },
      { name: 'kill_all_units', desc: window.rpT('rcon.game_force.kill_all_units') },
      { name: 'evolution_factor', desc: window.rpT('rcon.game_force.evolution_factor') },
      { name: 'manual_mining_speed_modifier', desc: window.rpT('rcon.game_force.manual_mining_speed_modifier') },
      { name: 'character_running_speed_modifier' },
      { name: 'worker_robots_speed_modifier' },
    ],
    'game.surface': [
      { name: 'name', desc: window.rpT('rcon.game_surface.name') },
      { name: 'index', desc: window.rpT('rcon.game_surface.index') },
      { name: 'create_entity', sig: 'create_entity{name=, position=, force=}' },
      { name: 'find_entity', sig: 'find_entity(name, position)' },
      { name: 'find_entities', sig: 'find_entities(area)' },
      { name: 'find_entities_filtered', sig: 'find_entities_filtered{area=, name=, type=, force=}' },
      { name: 'count_entities_filtered', sig: 'count_entities_filtered{...}' },
      { name: 'destroy_decoratives', sig: 'destroy_decoratives{area=}' },
      { name: 'spill_item_stack', sig: 'spill_item_stack{position=, stack=}' },
      { name: 'pollute', sig: 'pollute(position, amount)' },
      { name: 'request_to_generate_chunks', sig: 'request_to_generate_chunks(position, radius)' },
      { name: 'force_generate_chunk_requests' },
      { name: 'set_chunk_generated_status', sig: 'set_chunk_generated_status(chunk, status)' },
      { name: 'daytime', desc: window.rpT('rcon.game_surface.daytime') },
      { name: 'always_day', desc: window.rpT('rcon.game_surface.always_day') },
      { name: 'darkness', desc: window.rpT('rcon.game_surface.darkness') },
      { name: 'wind_speed', desc: window.rpT('rcon.game_surface.wind_speed') },
      { name: 'map_gen_settings', desc: window.rpT('rcon.game_surface.map_gen_settings') },
    ],
    'game.entity': [
      { name: 'name', desc: window.rpT('rcon.game_entity.name') },
      { name: 'type', desc: window.rpT('rcon.game_entity.type') },
      { name: 'position' },
      { name: 'health' },
      { name: 'force' },
      { name: 'surface' },
      { name: 'destroy', sig: 'destroy{raise_destroy=true}' },
      { name: 'die', sig: 'die([force], [cause])' },
      { name: 'teleport', sig: 'teleport(position)' },
      { name: 'get_inventory', sig: 'get_inventory(defines.inventory.*)' },
      { name: 'mine', sig: 'mine{inventory=, force=true}' },
      { name: 'rotate', sig: 'rotate{by_player=, reverse=}' },
      { name: 'minable', desc: window.rpT('rcon.game_entity.minable') },
      { name: 'valid', desc: window.rpT('rcon.game_entity.valid') },
      { name: 'unit_number', desc: window.rpT('rcon.game_entity.unit_number') },
    ],
    script: [
      { name: 'active_mods', desc: window.rpT('rcon.game_entity.active_mods') },
      { name: 'mod_name', desc: window.rpT('rcon.game_entity.mod_name') },
      { name: 'on_event', sig: 'on_event(event_id, handler)' },
      { name: 'on_init', sig: 'on_init(handler)' },
      { name: 'on_load', sig: 'on_load(handler)' },
      { name: 'on_nth_tick', sig: 'on_nth_tick(n, handler)' },
      { name: 'raise_event', sig: 'raise_event(event_id, data)' },
      { name: 'register_on_object_destroyed', sig: 'register_on_object_destroyed(obj)' },
      { name: 'level', desc: '{level_name, mod_name, campaign_name}' },
    ],
    remote: [
      { name: 'call', sig: 'call(interface_name, function_name, ...)' },
      { name: 'add_interface', sig: 'add_interface(name, {fn=function ... end, ...})' },
      { name: 'remove_interface', sig: 'remove_interface(name)' },
      { name: 'interfaces', desc: window.rpT('rcon.game_entity.interfaces') },
    ],
    defines: [
      { name: 'events', desc: window.rpT('rcon.game_entity.events') },
      { name: 'controllers', desc: 'character / god / ghost / spectator / cutscene / editor' },
      { name: 'inventory', desc: 'character_main / chest / fuel / armor / ...' },
      { name: 'direction', desc: 'north / east / south / west' },
      { name: 'difficulty', desc: 'easy / normal / hard' },
      { name: 'gui_type', desc: 'entity / research / production / ...' },
      { name: 'mouse_button_type', desc: 'left / right / middle' },
    ],
    rcon: [
      { name: 'print', sig: 'print(msg)', desc: window.rpT('rcon.game_entity.print') },
    ],
  };

  root.SLASH_CMDS = SLASH_CMDS;
  root.LUA_TOP = LUA_TOP;
  root.LUA_MEMBERS = LUA_MEMBERS;
})(window);
