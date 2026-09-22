# Inventaire du périmètre audité — 13 septembre 2026

Référence : `30a094f`. [Roadmap principale](../roadmap/ROADMAP_ANO_GPT_ULTIME_2026-09-13.md).

L'inventaire initial comporte 578 fichiers suivis, 442 fichiers Python dont 250 modules applicatifs (126 339 lignes). Tous les Python suivis ont été parsés avec AST. Les tests Python et Flutter et le lint sont décrits dans [l'annexe de validation](PREUVES_ET_VALIDATION.md).

Cette table indique la présence d'un test au nom identique ou préfixé par le nom du module. **Ce n'est pas une mesure de couverture**, et « — » ne signifie pas absence de tout test : de nombreux tests couvrent plusieurs modules sous un autre nom. La lecture approfondie est attestée par les symboles et preuves des fiches de roadmap ; la présence dans cet inventaire ne signifie pas audit manuel ligne par ligne.

## Modules Python applicatifs

| Module | Lignes | Tests de nom proche |
|---|---:|---|
| [actions/app_control.py](../../actions/app_control.py) | 699 | — |
| [actions/auto_debug.py](../../actions/auto_debug.py) | 119 | [test_auto_debug.py](../../tests/test_auto_debug.py) |
| [actions/background_tasks.py](../../actions/background_tasks.py) | 754 | [test_background_tasks.py](../../tests/test_background_tasks.py) |
| [actions/browser_control.py](../../actions/browser_control.py) | 1550 | — |
| [actions/browser_tab_control.py](../../actions/browser_tab_control.py) | 668 | — |
| [actions/calendar.py](../../actions/calendar.py) | 234 | [test_calendar_action.py](../../tests/test_calendar_action.py), [test_calendar_service.py](../../tests/test_calendar_service.py), [test_calendar_watcher.py](../../tests/test_calendar_watcher.py) |
| [actions/capability_guide.py](../../actions/capability_guide.py) | 889 | [test_capability_guide.py](../../tests/test_capability_guide.py) |
| [actions/capture.py](../../actions/capture.py) | 974 | [test_capture_action.py](../../tests/test_capture_action.py) |
| [actions/close_app.py](../../actions/close_app.py) | 1253 | — |
| [actions/close_app_smart.py](../../actions/close_app_smart.py) | 712 | — |
| [actions/cloud_integrations.py](../../actions/cloud_integrations.py) | 35 | — |
| [actions/code_helper.py](../../actions/code_helper.py) | 575 | [test_code_helper_safety.py](../../tests/test_code_helper_safety.py) |
| [actions/computer_control.py](../../actions/computer_control.py) | 1558 | [test_computer_control.py](../../tests/test_computer_control.py) |
| [actions/computer_settings.py](../../actions/computer_settings.py) | 1621 | — |
| [actions/contacts.py](../../actions/contacts.py) | 93 | [test_contacts.py](../../tests/test_contacts.py) |
| [actions/desktop.py](../../actions/desktop.py) | 839 | [test_desktop_action.py](../../tests/test_desktop_action.py) |
| [actions/desktop_apps.py](../../actions/desktop_apps.py) | 740 | — |
| [actions/dev_agent.py](../../actions/dev_agent.py) | 673 | — |
| [actions/devsecops.py](../../actions/devsecops.py) | 857 | [test_devsecops.py](../../tests/test_devsecops.py) |
| [actions/document_generation.py](../../actions/document_generation.py) | 202 | — |
| [actions/download_music.py](../../actions/download_music.py) | 933 | [test_download_music.py](../../tests/test_download_music.py) |
| [actions/email.py](../../actions/email.py) | 376 | [test_email_control_args.py](../../tests/test_email_control_args.py), [test_email_service.py](../../tests/test_email_service.py), [test_email_write_actions.py](../../tests/test_email_write_actions.py) |
| [actions/file_controller.py](../../actions/file_controller.py) | 1077 | — |
| [actions/file_processor.py](../../actions/file_processor.py) | 1259 | — |
| [actions/find_nearby.py](../../actions/find_nearby.py) | 145 | [test_find_nearby.py](../../tests/test_find_nearby.py), [test_find_nearby_location.py](../../tests/test_find_nearby_location.py) |
| [actions/flight_finder.py](../../actions/flight_finder.py) | 498 | — |
| [actions/game_updater.py](../../actions/game_updater.py) | 1041 | — |
| [actions/github.py](../../actions/github.py) | 369 | [test_github_integration.py](../../tests/test_github_integration.py) |
| [actions/hypr_orchestrator.py](../../actions/hypr_orchestrator.py) | 439 | [test_hypr_orchestrator.py](../../tests/test_hypr_orchestrator.py) |
| [actions/image_generation.py](../../actions/image_generation.py) | 34 | — |
| [actions/image_search.py](../../actions/image_search.py) | 340 | [test_image_search.py](../../tests/test_image_search.py) |
| [actions/launch_tracker.py](../../actions/launch_tracker.py) | 368 | — |
| [actions/media_control.py](../../actions/media_control.py) | 730 | [test_media_control.py](../../tests/test_media_control.py) |
| [actions/music.py](../../actions/music.py) | 1611 | [test_music_no_window.py](../../tests/test_music_no_window.py), [test_music_recognition.py](../../tests/test_music_recognition.py), [test_music_source_selection.py](../../tests/test_music_source_selection.py), [test_music_ui_layout.py](../../tests/test_music_ui_layout.py), [test_music_volume.py](../../tests/test_music_volume.py), [test_music_wake_word.py](../../tests/test_music_wake_word.py), [test_music_youtube_card.py](../../tests/test_music_youtube_card.py) |
| [actions/music_recognition.py](../../actions/music_recognition.py) | 474 | [test_music_recognition.py](../../tests/test_music_recognition.py) |
| [actions/navigation.py](../../actions/navigation.py) | 73 | [test_navigation.py](../../tests/test_navigation.py) |
| [actions/open_app.py](../../actions/open_app.py) | 1527 | [test_open_app_command.py](../../tests/test_open_app_command.py), [test_open_app_hidden.py](../../tests/test_open_app_hidden.py) |
| [actions/prayer.py](../../actions/prayer.py) | 140 | [test_prayer_times.py](../../tests/test_prayer_times.py) |
| [actions/proactive.py](../../actions/proactive.py) | 720 | [test_proactive_service.py](../../tests/test_proactive_service.py) |
| [actions/reminder.py](../../actions/reminder.py) | 927 | [test_reminder_parsing.py](../../tests/test_reminder_parsing.py), [test_reminder_system.py](../../tests/test_reminder_system.py) |
| [actions/screen_processor.py](../../actions/screen_processor.py) | 632 | — |
| [actions/second_brain.py](../../actions/second_brain.py) | 211 | [test_second_brain.py](../../tests/test_second_brain.py) |
| [actions/self_repair.py](../../actions/self_repair.py) | 88 | — |
| [actions/send_message.py](../../actions/send_message.py) | 604 | [test_send_message_args.py](../../tests/test_send_message_args.py) |
| [actions/shell_exec.py](../../actions/shell_exec.py) | 1154 | [test_shell_exec_safety.py](../../tests/test_shell_exec_safety.py) |
| [actions/smart_search.py](../../actions/smart_search.py) | 407 | — |
| [actions/sparring_partner.py](../../actions/sparring_partner.py) | 394 | [test_sparring_partner.py](../../tests/test_sparring_partner.py) |
| [actions/system_monitor.py](../../actions/system_monitor.py) | 604 | — |
| [actions/tiktok_coach.py](../../actions/tiktok_coach.py) | 1338 | [test_tiktok_coach.py](../../tests/test_tiktok_coach.py) |
| [actions/tiktok_tracker.py](../../actions/tiktok_tracker.py) | 605 | [test_tiktok_tracker.py](../../tests/test_tiktok_tracker.py) |
| [actions/video_generation.py](../../actions/video_generation.py) | 46 | — |
| [actions/visual_recognition.py](../../actions/visual_recognition.py) | 756 | — |
| [actions/weather_report.py](../../actions/weather_report.py) | 463 | — |
| [actions/web_control.py](../../actions/web_control.py) | 474 | — |
| [actions/web_search.py](../../actions/web_search.py) | 1198 | [test_web_search_geo.py](../../tests/test_web_search_geo.py), [test_web_search_hedge.py](../../tests/test_web_search_hedge.py) |
| [actions/window_instances.py](../../actions/window_instances.py) | 365 | — |
| [actions/youtube_video.py](../../actions/youtube_video.py) | 828 | — |
| [anogpt_mcp.py](../../anogpt_mcp.py) | 1058 | — |
| [config/__init__.py](../../config/__init__.py) | 26 | — |
| [core/__init__.py](../../core/__init__.py) | 18 | — |
| [core/action_kit.py](../../core/action_kit.py) | 952 | [test_action_kit.py](../../tests/test_action_kit.py) |
| [core/action_runtime.py](../../core/action_runtime.py) | 503 | [test_action_runtime.py](../../tests/test_action_runtime.py) |
| [core/agent_brain.py](../../core/agent_brain.py) | 257 | [test_agent_brain.py](../../tests/test_agent_brain.py) |
| [core/ai_stt_corrector.py](../../core/ai_stt_corrector.py) | 376 | — |
| [core/audio_capture.py](../../core/audio_capture.py) | 455 | [test_audio_capture.py](../../tests/test_audio_capture.py) |
| [core/audio_denoise.py](../../core/audio_denoise.py) | 511 | [test_audio_denoise.py](../../tests/test_audio_denoise.py) |
| [core/audio_engine.py](../../core/audio_engine.py) | 2135 | — |
| [core/audio_router.py](../../core/audio_router.py) | 802 | [test_audio_router.py](../../tests/test_audio_router.py) |
| [core/audio_vad.py](../../core/audio_vad.py) | 378 | [test_audio_vad.py](../../tests/test_audio_vad.py) |
| [core/auto_debug.py](../../core/auto_debug.py) | 941 | [test_auto_debug.py](../../tests/test_auto_debug.py) |
| [core/auto_extension.py](../../core/auto_extension.py) | 286 | [test_auto_extension.py](../../tests/test_auto_extension.py) |
| [core/auto_fix.py](../../core/auto_fix.py) | 222 | — |
| [core/auto_persona.py](../../core/auto_persona.py) | 28 | [test_auto_persona.py](../../tests/test_auto_persona.py) |
| [core/azure_specialists.py](../../core/azure_specialists.py) | 312 | — |
| [core/azure_speech_stt.py](../../core/azure_speech_stt.py) | 258 | [test_azure_speech_stt.py](../../tests/test_azure_speech_stt.py) |
| [core/background_task.py](../../core/background_task.py) | 53 | [test_background_task_cards.py](../../tests/test_background_task_cards.py) |
| [core/barge_in.py](../../core/barge_in.py) | 441 | [test_barge_in.py](../../tests/test_barge_in.py) |
| [core/brain_relay.py](../../core/brain_relay.py) | 265 | [test_brain_relay.py](../../tests/test_brain_relay.py) |
| [core/browser_policy.py](../../core/browser_policy.py) | 65 | [test_browser_policy.py](../../tests/test_browser_policy.py) |
| [core/calendar_service.py](../../core/calendar_service.py) | 496 | [test_calendar_service.py](../../tests/test_calendar_service.py) |
| [core/calendar_watcher.py](../../core/calendar_watcher.py) | 205 | [test_calendar_watcher.py](../../tests/test_calendar_watcher.py) |
| [core/camera_studio.py](../../core/camera_studio.py) | 519 | [test_camera_studio.py](../../tests/test_camera_studio.py) |
| [core/cloud_integrations.py](../../core/cloud_integrations.py) | 174 | — |
| [core/contacts.py](../../core/contacts.py) | 235 | [test_contacts.py](../../tests/test_contacts.py) |
| [core/context_probe.py](../../core/context_probe.py) | 330 | [test_context_probe.py](../../tests/test_context_probe.py) |
| [core/continuous_conversation.py](../../core/continuous_conversation.py) | 302 | [test_continuous_conversation.py](../../tests/test_continuous_conversation.py) |
| [core/continuous_vision.py](../../core/continuous_vision.py) | 1234 | [test_continuous_vision.py](../../tests/test_continuous_vision.py) |
| [core/conversation_language.py](../../core/conversation_language.py) | 59 | [test_conversation_language.py](../../tests/test_conversation_language.py) |
| [core/daily_briefing.py](../../core/daily_briefing.py) | 394 | [test_daily_briefing.py](../../tests/test_daily_briefing.py) |
| [core/decision_simulator.py](../../core/decision_simulator.py) | 197 | [test_decision_simulator.py](../../tests/test_decision_simulator.py) |
| [core/diagnostics.py](../../core/diagnostics.py) | 76 | — |
| [core/distraction_guard.py](../../core/distraction_guard.py) | 387 | [test_distraction_guard.py](../../tests/test_distraction_guard.py) |
| [core/double_talk_detector.py](../../core/double_talk_detector.py) | 627 | [test_double_talk_detector.py](../../tests/test_double_talk_detector.py) |
| [core/echo_canceller.py](../../core/echo_canceller.py) | 825 | [test_echo_canceller.py](../../tests/test_echo_canceller.py) |
| [core/elevenlabs_voice.py](../../core/elevenlabs_voice.py) | 119 | [test_elevenlabs_voice.py](../../tests/test_elevenlabs_voice.py) |
| [core/email_service.py](../../core/email_service.py) | 1396 | [test_email_service.py](../../tests/test_email_service.py) |
| [core/event_bus.py](../../core/event_bus.py) | 909 | [test_event_bus.py](../../tests/test_event_bus.py) |
| [core/face_memory.py](../../core/face_memory.py) | 886 | [test_face_memory.py](../../tests/test_face_memory.py) |
| [core/file_indexer.py](../../core/file_indexer.py) | 367 | [test_file_indexer.py](../../tests/test_file_indexer.py) |
| [core/freeze_watch.py](../../core/freeze_watch.py) | 125 | [test_freeze_watch_cumulative.py](../../tests/test_freeze_watch_cumulative.py) |
| [core/gemini_connection.py](../../core/gemini_connection.py) | 164 | — |
| [core/gemini_transcribe_stt.py](../../core/gemini_transcribe_stt.py) | 679 | [test_gemini_transcribe_stt.py](../../tests/test_gemini_transcribe_stt.py) |
| [core/geolocation.py](../../core/geolocation.py) | 314 | [test_geolocation.py](../../tests/test_geolocation.py) |
| [core/gesture_control.py](../../core/gesture_control.py) | 1015 | [test_gesture_control.py](../../tests/test_gesture_control.py) |
| [core/ghost_agent.py](../../core/ghost_agent.py) | 284 | [test_ghost_agent.py](../../tests/test_ghost_agent.py) |
| [core/github_backup.py](../../core/github_backup.py) | 41 | — |
| [core/github_service.py](../../core/github_service.py) | 216 | — |
| [core/habit_model.py](../../core/habit_model.py) | 165 | [test_habit_model.py](../../tests/test_habit_model.py) |
| [core/human_confirmation.py](../../core/human_confirmation.py) | 197 | [test_human_confirmation.py](../../tests/test_human_confirmation.py), [test_human_confirmation_card.py](../../tests/test_human_confirmation_card.py) |
| [core/hypr_focus.py](../../core/hypr_focus.py) | 203 | [test_hypr_focus.py](../../tests/test_hypr_focus.py) |
| [core/incident_log.py](../../core/incident_log.py) | 224 | — |
| [core/installer.py](../../core/installer.py) | 308 | — |
| [core/interrupt_worker.py](../../core/interrupt_worker.py) | 92 | — |
| [core/ipc.py](../../core/ipc.py) | 160 | — |
| [core/isolated_interrupt.py](../../core/isolated_interrupt.py) | 163 | — |
| [core/knowledge_graph.py](../../core/knowledge_graph.py) | 893 | [test_knowledge_graph.py](../../tests/test_knowledge_graph.py) |
| [core/live_captions.py](../../core/live_captions.py) | 173 | [test_live_captions.py](../../tests/test_live_captions.py) |
| [core/live_model_policy.py](../../core/live_model_policy.py) | 101 | [test_live_model_policy.py](../../tests/test_live_model_policy.py) |
| [core/live_speech_config.py](../../core/live_speech_config.py) | 169 | [test_live_speech_config.py](../../tests/test_live_speech_config.py) |
| [core/llm_client.py](../../core/llm_client.py) | 2283 | [test_llm_client_gemini_afc.py](../../tests/test_llm_client_gemini_afc.py) |
| [core/local_video.py](../../core/local_video.py) | 101 | [test_local_video_player.py](../../tests/test_local_video_player.py) |
| [core/map_render.py](../../core/map_render.py) | 1224 | [test_map_render.py](../../tests/test_map_render.py) |
| [core/media_search.py](../../core/media_search.py) | 284 | [test_media_search.py](../../tests/test_media_search.py) |
| [core/memory_episode.py](../../core/memory_episode.py) | 155 | — |
| [core/memory_store.py](../../core/memory_store.py) | 620 | — |
| [core/migrated_tools.py](../../core/migrated_tools.py) | 392 | — |
| [core/multimodal_vision.py](../../core/multimodal_vision.py) | 731 | [test_multimodal_vision.py](../../tests/test_multimodal_vision.py) |
| [core/navigation.py](../../core/navigation.py) | 1219 | [test_navigation.py](../../tests/test_navigation.py) |
| [core/noise_suppressor.py](../../core/noise_suppressor.py) | 526 | [test_noise_suppressor.py](../../tests/test_noise_suppressor.py) |
| [core/observability.py](../../core/observability.py) | 245 | — |
| [core/persona_manager.py](../../core/persona_manager.py) | 601 | [test_persona_manager.py](../../tests/test_persona_manager.py) |
| [core/personal_rag.py](../../core/personal_rag.py) | 1902 | [test_personal_rag.py](../../tests/test_personal_rag.py) |
| [core/personality_modes.py](../../core/personality_modes.py) | 442 | [test_personality_modes.py](../../tests/test_personality_modes.py) |
| [core/phone_numbers.py](../../core/phone_numbers.py) | 73 | — |
| [core/phone_relay.py](../../core/phone_relay.py) | 491 | — |
| [core/places.py](../../core/places.py) | 503 | [test_places.py](../../tests/test_places.py) |
| [core/player_ipc.py](../../core/player_ipc.py) | 421 | — |
| [core/plugin_registry.py](../../core/plugin_registry.py) | 187 | [test_plugin_registry.py](../../tests/test_plugin_registry.py) |
| [core/plugin_sdk.py](../../core/plugin_sdk.py) | 16 | — |
| [core/prayer_times.py](../../core/prayer_times.py) | 538 | [test_prayer_times.py](../../tests/test_prayer_times.py) |
| [core/precision_stt.py](../../core/precision_stt.py) | 394 | [test_precision_stt.py](../../tests/test_precision_stt.py), [test_precision_stt_pipeline.py](../../tests/test_precision_stt_pipeline.py) |
| [core/proactive_engine.py](../../core/proactive_engine.py) | 827 | — |
| [core/prosody.py](../../core/prosody.py) | 568 | [test_prosody.py](../../tests/test_prosody.py), [test_prosody_analyzer.py](../../tests/test_prosody_analyzer.py) |
| [core/prosody_analyzer.py](../../core/prosody_analyzer.py) | 766 | [test_prosody_analyzer.py](../../tests/test_prosody_analyzer.py) |
| [core/routines.py](../../core/routines.py) | 262 | — |
| [core/screen_capture.py](../../core/screen_capture.py) | 418 | [test_screen_capture.py](../../tests/test_screen_capture.py) |
| [core/screen_consciousness.py](../../core/screen_consciousness.py) | 1350 | [test_screen_consciousness.py](../../tests/test_screen_consciousness.py) |
| [core/screen_reader.py](../../core/screen_reader.py) | 225 | [test_screen_reader.py](../../tests/test_screen_reader.py) |
| [core/scribe_stt.py](../../core/scribe_stt.py) | 336 | [test_scribe_stt.py](../../tests/test_scribe_stt.py) |
| [core/self_healing.py](../../core/self_healing.py) | 212 | [test_self_healing.py](../../tests/test_self_healing.py) |
| [core/semantic_enricher.py](../../core/semantic_enricher.py) | 294 | — |
| [core/session_manager.py](../../core/session_manager.py) | 1731 | — |
| [core/spatial_audio.py](../../core/spatial_audio.py) | 1135 | [test_spatial_audio.py](../../tests/test_spatial_audio.py) |
| [core/speaker_id.py](../../core/speaker_id.py) | 300 | — |
| [core/speech_sync.py](../../core/speech_sync.py) | 79 | — |
| [core/storage_maintenance.py](../../core/storage_maintenance.py) | 203 | — |
| [core/stt.py](../../core/stt.py) | 1198 | [test_stt_capture_fidelity.py](../../tests/test_stt_capture_fidelity.py), [test_stt_endpointing.py](../../tests/test_stt_endpointing.py) |
| [core/stt_audio.py](../../core/stt_audio.py) | 32 | — |
| [core/text_clean.py](../../core/text_clean.py) | 54 | [test_text_clean.py](../../tests/test_text_clean.py) |
| [core/thought_streamer.py](../../core/thought_streamer.py) | 821 | [test_thought_streamer.py](../../tests/test_thought_streamer.py) |
| [core/thread_pool.py](../../core/thread_pool.py) | 1209 | [test_thread_pool.py](../../tests/test_thread_pool.py) |
| [core/timers.py](../../core/timers.py) | 63 | [test_timers.py](../../tests/test_timers.py) |
| [core/tool_bridge.py](../../core/tool_bridge.py) | 156 | [test_tool_bridge.py](../../tests/test_tool_bridge.py) |
| [core/tool_dispatcher.py](../../core/tool_dispatcher.py) | 4838 | — |
| [core/tool_packs.py](../../core/tool_packs.py) | 285 | [test_tool_packs.py](../../tests/test_tool_packs.py) |
| [core/tool_registry.py](../../core/tool_registry.py) | 1231 | [test_tool_registry.py](../../tests/test_tool_registry.py) |
| [core/tool_stats.py](../../core/tool_stats.py) | 257 | — |
| [core/tool_utils.py](../../core/tool_utils.py) | 333 | — |
| [core/tts.py](../../core/tts.py) | 755 | — |
| [core/undo_stack.py](../../core/undo_stack.py) | 70 | [test_undo_stack.py](../../tests/test_undo_stack.py) |
| [core/vad_silero.py](../../core/vad_silero.py) | 171 | [test_vad_silero.py](../../tests/test_vad_silero.py) |
| [core/vector_memory.py](../../core/vector_memory.py) | 1609 | [test_vector_memory_batching.py](../../tests/test_vector_memory_batching.py) |
| [core/wake_word.py](../../core/wake_word.py) | 361 | — |
| [core/youtube_service.py](../../core/youtube_service.py) | 346 | — |
| [core/zapzap_controller.py](../../core/zapzap_controller.py) | 199 | [test_zapzap_controller.py](../../tests/test_zapzap_controller.py) |
| [dashboard/__init__.py](../../dashboard/__init__.py) | 0 | — |
| [dashboard/server.py](../../dashboard/server.py) | 1772 | — |
| [main.py](../../main.py) | 2141 | — |
| [memory/__init__.py](../../memory/__init__.py) | 1 | — |
| [memory/config_manager.py](../../memory/config_manager.py) | 535 | — |
| [memory/memory_manager.py](../../memory/memory_manager.py) | 471 | — |
| [plugins/_template.py](../../plugins/_template.py) | 17 | — |
| [plugins/analyseur_projet.py](../../plugins/analyseur_projet.py) | 898 | — |
| [setup.py](../../setup.py) | 26 | — |
| [ui/__init__.py](../../ui/__init__.py) | 82 | — |
| [ui/core/__init__.py](../../ui/core/__init__.py) | 2 | — |
| [ui/core/background_image.py](../../ui/core/background_image.py) | 84 | — |
| [ui/core/fade_widget.py](../../ui/core/fade_widget.py) | 125 | — |
| [ui/core/hud_button.py](../../ui/core/hud_button.py) | 162 | — |
| [ui/core/hud_paint.py](../../ui/core/hud_paint.py) | 224 | — |
| [ui/core/metrics.py](../../ui/core/metrics.py) | 196 | — |
| [ui/core/qtflags.py](../../ui/core/qtflags.py) | 56 | — |
| [ui/core/speech_text.py](../../ui/core/speech_text.py) | 28 | [test_speech_text_sync.py](../../tests/test_speech_text_sync.py) |
| [ui/dialogs/__init__.py](../../ui/dialogs/__init__.py) | 2 | — |
| [ui/dialogs/ai_config.py](../../ui/dialogs/ai_config.py) | 994 | — |
| [ui/dialogs/audio.py](../../ui/dialogs/audio.py) | 473 | [test_audio_capture.py](../../tests/test_audio_capture.py), [test_audio_denoise.py](../../tests/test_audio_denoise.py), [test_audio_router.py](../../tests/test_audio_router.py), [test_audio_vad.py](../../tests/test_audio_vad.py), [test_audio_visual_regressions.py](../../tests/test_audio_visual_regressions.py), [test_audio_watchdog.py](../../tests/test_audio_watchdog.py) |
| [ui/dialogs/customize.py](../../ui/dialogs/customize.py) | 329 | — |
| [ui/dialogs/memory.py](../../ui/dialogs/memory.py) | 73 | — |
| [ui/dialogs/plugins.py](../../ui/dialogs/plugins.py) | 72 | — |
| [ui/dialogs/remote.py](../../ui/dialogs/remote.py) | 208 | — |
| [ui/dialogs/setup.py](../../ui/dialogs/setup.py) | 116 | — |
| [ui/jarvis_ui.py](../../ui/jarvis_ui.py) | 409 | — |
| [ui/main_window.py](../../ui/main_window.py) | 181 | — |
| [ui/media/__init__.py](../../ui/media/__init__.py) | 2 | — |
| [ui/media/camera.py](../../ui/media/camera.py) | 74 | [test_camera_overlay.py](../../tests/test_camera_overlay.py), [test_camera_studio.py](../../tests/test_camera_studio.py), [test_camera_tool.py](../../tests/test_camera_tool.py) |
| [ui/media/gallery.py](../../ui/media/gallery.py) | 390 | — |
| [ui/media/generated_image_preview.py](../../ui/media/generated_image_preview.py) | 115 | — |
| [ui/media/map_views.py](../../ui/media/map_views.py) | 60 | — |
| [ui/media/video_hub.py](../../ui/media/video_hub.py) | 431 | [test_video_hub.py](../../tests/test_video_hub.py) |
| [ui/media/video_playback.py](../../ui/media/video_playback.py) | 440 | — |
| [ui/media/video_widgets.py](../../ui/media/video_widgets.py) | 183 | — |
| [ui/orb/__init__.py](../../ui/orb/__init__.py) | 33 | — |
| [ui/orb/arc_core.py](../../ui/orb/arc_core.py) | 996 | — |
| [ui/orb/arc_paint.py](../../ui/orb/arc_paint.py) | 491 | — |
| [ui/orb/arc_sprites.py](../../ui/orb/arc_sprites.py) | 85 | — |
| [ui/orb/companion.py](../../ui/orb/companion.py) | 170 | — |
| [ui/orb/glsl_orb.py](../../ui/orb/glsl_orb.py) | 909 | [test_glsl_orb.py](../../tests/test_glsl_orb.py) |
| [ui/orb/input_shape.py](../../ui/orb/input_shape.py) | 40 | — |
| [ui/orb/mini_orb.py](../../ui/orb/mini_orb.py) | 236 | [test_mini_orb.py](../../tests/test_mini_orb.py) |
| [ui/orb/radial_waveform.py](../../ui/orb/radial_waveform.py) | 1193 | [test_radial_waveform.py](../../tests/test_radial_waveform.py) |
| [ui/panels/__init__.py](../../ui/panels/__init__.py) | 2 | — |
| [ui/panels/cards_stack.py](../../ui/panels/cards_stack.py) | 389 | — |
| [ui/panels/clipboard.py](../../ui/panels/clipboard.py) | 107 | — |
| [ui/panels/drop.py](../../ui/panels/drop.py) | 266 | — |
| [ui/panels/file_chip.py](../../ui/panels/file_chip.py) | 121 | — |
| [ui/panels/floating_panel.py](../../ui/panels/floating_panel.py) | 206 | — |
| [ui/panels/interface_frame.py](../../ui/panels/interface_frame.py) | 124 | — |
| [ui/panels/log_widget.py](../../ui/panels/log_widget.py) | 181 | — |
| [ui/panels/music_player.py](../../ui/panels/music_player.py) | 172 | — |
| [ui/panels/music_widgets.py](../../ui/panels/music_widgets.py) | 353 | — |
| [ui/panels/rich_card_system.py](../../ui/panels/rich_card_system.py) | 2855 | [test_rich_card_system.py](../../tests/test_rich_card_system.py) |
| [ui/panels/speech_overlay.py](../../ui/panels/speech_overlay.py) | 265 | [test_speech_overlay.py](../../tests/test_speech_overlay.py) |
| [ui/panels/status_pill.py](../../ui/panels/status_pill.py) | 97 | — |
| [ui/panels/telemetry.py](../../ui/panels/telemetry.py) | 243 | — |
| [ui/panels/thought_overlay.py](../../ui/panels/thought_overlay.py) | 268 | — |
| [ui/paths.py](../../ui/paths.py) | 98 | — |
| [ui/styles/__init__.py](../../ui/styles/__init__.py) | 2 | — |
| [ui/styles/cyber.py](../../ui/styles/cyber.py) | 304 | — |
| [ui/styles/qss.py](../../ui/styles/qss.py) | 167 | — |
| [ui/styles/theme.py](../../ui/styles/theme.py) | 233 | — |
| [ui/visual_pointer.py](../../ui/visual_pointer.py) | 1347 | [test_visual_pointer.py](../../tests/test_visual_pointer.py) |
| [ui/window/__init__.py](../../ui/window/__init__.py) | 2 | — |
| [ui/window/chrome.py](../../ui/window/chrome.py) | 421 | — |
| [ui/window/dialogs_host.py](../../ui/window/dialogs_host.py) | 399 | — |
| [ui/window/drawer.py](../../ui/window/drawer.py) | 282 | — |
| [ui/window/media_host.py](../../ui/window/media_host.py) | 484 | — |
| [ui/window/overlay_layout.py](../../ui/window/overlay_layout.py) | 239 | — |
| [ui/window/positions.py](../../ui/window/positions.py) | 190 | — |
| [ui/window/scene.py](../../ui/window/scene.py) | 473 | — |
| [ui/window/system_ops.py](../../ui/window/system_ops.py) | 430 | — |

## Scripts et exemples Python

Ces fichiers entrent dans le recensement AST ; ils ne sont pas tous destinés à la production ni couverts par Ruff avec les mêmes exclusions.

| Fichier | Lignes |
|---|---:|
| [development/calculatrice_simple.py](../../development/calculatrice_simple.py) | 6 |
| [scripts/_extract_ui_package.py](../../scripts/_extract_ui_package.py) | 469 |
| [scripts/anogpt-maintenance.py](../../scripts/anogpt-maintenance.py) | 76 |
| [scripts/audit_silent_handlers.py](../../scripts/audit_silent_handlers.py) | 62 |
| [scripts/azure_deploy_models.py](../../scripts/azure_deploy_models.py) | 342 |
| [scripts/benchmark_event_bus.py](../../scripts/benchmark_event_bus.py) | 240 |
| [scripts/benchmark_screen_consciousness.py](../../scripts/benchmark_screen_consciousness.py) | 30 |
| [scripts/calibrate_wake_word.py](../../scripts/calibrate_wake_word.py) | 112 |
| [scripts/check_audio_chain.py](../../scripts/check_audio_chain.py) | 96 |
| [scripts/demo_personal_rag.py](../../scripts/demo_personal_rag.py) | 199 |
| [scripts/diagnose_stt.py](../../scripts/diagnose_stt.py) | 107 |
| [scripts/migrate_to_vector_memory.py](../../scripts/migrate_to_vector_memory.py) | 293 |
| [scripts/preview_interface.py](../../scripts/preview_interface.py) | 87 |
| [scripts/preview_mini_orb.py](../../scripts/preview_mini_orb.py) | 166 |
| [scripts/preview_orb.py](../../scripts/preview_orb.py) | 143 |
| [scripts/setup_cloud_token.py](../../scripts/setup_cloud_token.py) | 54 |
| [scripts/setup_gmail.py](../../scripts/setup_gmail.py) | 73 |

## Client Flutter et Android

Tous les fichiers Dart de l'application ont été soumis à `flutter analyze`. Les 20 tests Flutter passent. Les trois sources Kotlin ont été lues sur leurs chemins de permissions, connexion, appel/SMS et cycle de vie ; **le build Kotlin et la recette sur appareil n'ont pas été exécutés**.

| Fichier | Lignes |
|---|---:|
| [mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt](../../mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt) | 580 |
| [mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/PhoneRelayService.kt](../../mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/PhoneRelayService.kt) | 291 |
| [mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/SmsReceiver.kt](../../mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/SmsReceiver.kt) | 62 |
| [mobile/ano_remote/lib/camera_link.dart](../../mobile/ano_remote/lib/camera_link.dart) | 214 |
| [mobile/ano_remote/lib/home_page.dart](../../mobile/ano_remote/lib/home_page.dart) | 834 |
| [mobile/ano_remote/lib/location_link.dart](../../mobile/ano_remote/lib/location_link.dart) | 263 |
| [mobile/ano_remote/lib/main.dart](../../mobile/ano_remote/lib/main.dart) | 47 |
| [mobile/ano_remote/lib/net.dart](../../mobile/ano_remote/lib/net.dart) | 417 |
| [mobile/ano_remote/lib/pairing_page.dart](../../mobile/ano_remote/lib/pairing_page.dart) | 262 |
| [mobile/ano_remote/lib/phone_link.dart](../../mobile/ano_remote/lib/phone_link.dart) | 212 |
| [mobile/ano_remote/lib/scanner_page.dart](../../mobile/ano_remote/lib/scanner_page.dart) | 161 |
| [mobile/ano_remote/lib/session.dart](../../mobile/ano_remote/lib/session.dart) | 487 |
| [mobile/ano_remote/lib/theme.dart](../../mobile/ano_remote/lib/theme.dart) | 107 |
| [mobile/ano_remote/lib/voice_link.dart](../../mobile/ano_remote/lib/voice_link.dart) | 190 |
| [mobile/ano_remote/lib/voice_orb.dart](../../mobile/ano_remote/lib/voice_orb.dart) | 246 |
| [mobile/ano_remote/test/camera_lens_test.dart](../../mobile/ano_remote/test/camera_lens_test.dart) | 42 |
| [mobile/ano_remote/test/location_link_test.dart](../../mobile/ano_remote/test/location_link_test.dart) | 55 |
| [mobile/ano_remote/test/session_test.dart](../../mobile/ano_remote/test/session_test.dart) | 49 |
| [mobile/ano_remote/test/voice_orb_test.dart](../../mobile/ano_remote/test/voice_orb_test.dart) | 38 |
| [mobile/ano_remote/test/widget_test.dart](../../mobile/ano_remote/test/widget_test.dart) | 69 |

## Interfaces web et livraison

Serveur FastAPI inspecté, routes et contrats d'authentification testés avec faux états. HTML courant inspecté notamment pour le stockage des clés et le rendu dynamique. Le bundle compilé n'a pas fait l'objet d'un audit JavaScript complet ; sa reproductibilité fait l'objet de C45.

- [.github/workflows/quality.yml](../../.github/workflows/quality.yml)
- [.pre-commit-config.yaml](../../.pre-commit-config.yaml)
- [config/systemd/anogpt.service](../../config/systemd/anogpt.service)
- [dashboard/dist/assets/index-Cc_YMoBD.css](../../dashboard/dist/assets/index-Cc_YMoBD.css)
- [dashboard/dist/assets/index-DJRBGerv.js](../../dashboard/dist/assets/index-DJRBGerv.js)
- [dashboard/dist/index.html](../../dashboard/dist/index.html)
- [dashboard/static/app.html](../../dashboard/static/app.html)
- [dashboard/static/app.legacy.html](../../dashboard/static/app.legacy.html)
- [dashboard/static/crypto-js.min.js](../../dashboard/static/crypto-js.min.js)
- [dashboard/static/login.html](../../dashboard/static/login.html)
- [pyproject.toml](../../pyproject.toml)
- [requirements.txt](../../requirements.txt)
- [scripts/install-systemd-unit.sh](../../scripts/install-systemd-unit.sh)
- [setup.py](../../setup.py)

## Fichiers de tests Python

175 fichiers Python dans `tests/`, dont l'initialiseur. La suite a collecté 1 692 cas : 1 663 passent, 5 échouent, 23 sont ignorés et 1 échoue de manière attendue. Ces tests comportent des mocks et des assertions statiques ; ils ne certifient pas toutes les intégrations externes.

- [tests/test_action_kit.py](../../tests/test_action_kit.py)
- [tests/test_action_runtime.py](../../tests/test_action_runtime.py)
- [tests/test_agent_brain.py](../../tests/test_agent_brain.py)
- [tests/test_agent_delegation_prompt.py](../../tests/test_agent_delegation_prompt.py)
- [tests/test_audio_capture.py](../../tests/test_audio_capture.py)
- [tests/test_audio_denoise.py](../../tests/test_audio_denoise.py)
- [tests/test_audio_router.py](../../tests/test_audio_router.py)
- [tests/test_audio_vad.py](../../tests/test_audio_vad.py)
- [tests/test_audio_visual_regressions.py](../../tests/test_audio_visual_regressions.py)
- [tests/test_audio_watchdog.py](../../tests/test_audio_watchdog.py)
- [tests/test_auto_debug.py](../../tests/test_auto_debug.py)
- [tests/test_auto_extension.py](../../tests/test_auto_extension.py)
- [tests/test_auto_persona.py](../../tests/test_auto_persona.py)
- [tests/test_azure_speech_stt.py](../../tests/test_azure_speech_stt.py)
- [tests/test_background_task_cards.py](../../tests/test_background_task_cards.py)
- [tests/test_background_tasks.py](../../tests/test_background_tasks.py)
- [tests/test_barge_in.py](../../tests/test_barge_in.py)
- [tests/test_brain_relay.py](../../tests/test_brain_relay.py)
- [tests/test_browser_policy.py](../../tests/test_browser_policy.py)
- [tests/test_calendar_action.py](../../tests/test_calendar_action.py)
- [tests/test_calendar_service.py](../../tests/test_calendar_service.py)
- [tests/test_calendar_watcher.py](../../tests/test_calendar_watcher.py)
- [tests/test_camera_overlay.py](../../tests/test_camera_overlay.py)
- [tests/test_camera_studio.py](../../tests/test_camera_studio.py)
- [tests/test_camera_tool.py](../../tests/test_camera_tool.py)
- [tests/test_capability_guide.py](../../tests/test_capability_guide.py)
- [tests/test_capture_action.py](../../tests/test_capture_action.py)
- [tests/test_code_helper_safety.py](../../tests/test_code_helper_safety.py)
- [tests/test_computer_control.py](../../tests/test_computer_control.py)
- [tests/test_contacts.py](../../tests/test_contacts.py)
- [tests/test_context_probe.py](../../tests/test_context_probe.py)
- [tests/test_continuous_conversation.py](../../tests/test_continuous_conversation.py)
- [tests/test_continuous_vision.py](../../tests/test_continuous_vision.py)
- [tests/test_conversation_language.py](../../tests/test_conversation_language.py)
- [tests/test_daily_briefing.py](../../tests/test_daily_briefing.py)
- [tests/test_daily_news_focus.py](../../tests/test_daily_news_focus.py)
- [tests/test_dashboard_network.py](../../tests/test_dashboard_network.py)
- [tests/test_dashboard_reliability.py](../../tests/test_dashboard_reliability.py)
- [tests/test_decision_simulator.py](../../tests/test_decision_simulator.py)
- [tests/test_desktop_action.py](../../tests/test_desktop_action.py)
- [tests/test_devsecops.py](../../tests/test_devsecops.py)
- [tests/test_distraction_guard.py](../../tests/test_distraction_guard.py)
- [tests/test_double_talk_detector.py](../../tests/test_double_talk_detector.py)
- [tests/test_download_music.py](../../tests/test_download_music.py)
- [tests/test_durability.py](../../tests/test_durability.py)
- [tests/test_echo_canceller.py](../../tests/test_echo_canceller.py)
- [tests/test_elevenlabs_voice.py](../../tests/test_elevenlabs_voice.py)
- [tests/test_email_control_args.py](../../tests/test_email_control_args.py)
- [tests/test_email_service.py](../../tests/test_email_service.py)
- [tests/test_email_write_actions.py](../../tests/test_email_write_actions.py)
- [tests/test_error_visibility.py](../../tests/test_error_visibility.py)
- [tests/test_event_bus.py](../../tests/test_event_bus.py)
- [tests/test_face_memory.py](../../tests/test_face_memory.py)
- [tests/test_file_indexer.py](../../tests/test_file_indexer.py)
- [tests/test_file_search_routing.py](../../tests/test_file_search_routing.py)
- [tests/test_file_undo.py](../../tests/test_file_undo.py)
- [tests/test_find_nearby.py](../../tests/test_find_nearby.py)
- [tests/test_find_nearby_location.py](../../tests/test_find_nearby_location.py)
- [tests/test_freeze_watch_cumulative.py](../../tests/test_freeze_watch_cumulative.py)
- [tests/test_gemini_transcribe_stt.py](../../tests/test_gemini_transcribe_stt.py)
- [tests/test_geolocation.py](../../tests/test_geolocation.py)
- [tests/test_gesture_control.py](../../tests/test_gesture_control.py)
- [tests/test_ghost_agent.py](../../tests/test_ghost_agent.py)
- [tests/test_github_integration.py](../../tests/test_github_integration.py)
- [tests/test_glsl_orb.py](../../tests/test_glsl_orb.py)
- [tests/test_habit_model.py](../../tests/test_habit_model.py)
- [tests/test_hud_chrome.py](../../tests/test_hud_chrome.py)
- [tests/test_human_confirmation.py](../../tests/test_human_confirmation.py)
- [tests/test_human_confirmation_card.py](../../tests/test_human_confirmation_card.py)
- [tests/test_hypr_focus.py](../../tests/test_hypr_focus.py)
- [tests/test_hypr_orchestrator.py](../../tests/test_hypr_orchestrator.py)
- [tests/test_image_gallery.py](../../tests/test_image_gallery.py)
- [tests/test_image_search.py](../../tests/test_image_search.py)
- [tests/test_incident_autofix.py](../../tests/test_incident_autofix.py)
- [tests/test_interface_shell.py](../../tests/test_interface_shell.py)
- [tests/test_interrupt_recovery.py](../../tests/test_interrupt_recovery.py)
- [tests/test_interrupt_scope.py](../../tests/test_interrupt_scope.py)
- [tests/test_knowledge_graph.py](../../tests/test_knowledge_graph.py)
- [tests/test_live_captions.py](../../tests/test_live_captions.py)
- [tests/test_live_liveness.py](../../tests/test_live_liveness.py)
- [tests/test_live_model_policy.py](../../tests/test_live_model_policy.py)
- [tests/test_live_position.py](../../tests/test_live_position.py)
- [tests/test_live_speech_config.py](../../tests/test_live_speech_config.py)
- [tests/test_llm_brain_routing.py](../../tests/test_llm_brain_routing.py)
- [tests/test_llm_client_gemini_afc.py](../../tests/test_llm_client_gemini_afc.py)
- [tests/test_local_barge_in.py](../../tests/test_local_barge_in.py)
- [tests/test_local_video_player.py](../../tests/test_local_video_player.py)
- [tests/test_location_context.py](../../tests/test_location_context.py)
- [tests/test_manual_microphone_lock.py](../../tests/test_manual_microphone_lock.py)
- [tests/test_map_render.py](../../tests/test_map_render.py)
- [tests/test_mcp_tools.py](../../tests/test_mcp_tools.py)
- [tests/test_media_control.py](../../tests/test_media_control.py)
- [tests/test_media_search.py](../../tests/test_media_search.py)
- [tests/test_mini_orb.py](../../tests/test_mini_orb.py)
- [tests/test_multimodal_vision.py](../../tests/test_multimodal_vision.py)
- [tests/test_music_no_window.py](../../tests/test_music_no_window.py)
- [tests/test_music_recognition.py](../../tests/test_music_recognition.py)
- [tests/test_music_source_selection.py](../../tests/test_music_source_selection.py)
- [tests/test_music_ui_layout.py](../../tests/test_music_ui_layout.py)
- [tests/test_music_volume.py](../../tests/test_music_volume.py)
- [tests/test_music_wake_word.py](../../tests/test_music_wake_word.py)
- [tests/test_music_youtube_card.py](../../tests/test_music_youtube_card.py)
- [tests/test_navigation.py](../../tests/test_navigation.py)
- [tests/test_neural_speech_gate.py](../../tests/test_neural_speech_gate.py)
- [tests/test_noise_suppressor.py](../../tests/test_noise_suppressor.py)
- [tests/test_open_app_command.py](../../tests/test_open_app_command.py)
- [tests/test_open_app_hidden.py](../../tests/test_open_app_hidden.py)
- [tests/test_orb_particle_cloud.py](../../tests/test_orb_particle_cloud.py)
- [tests/test_persona_manager.py](../../tests/test_persona_manager.py)
- [tests/test_personal_rag.py](../../tests/test_personal_rag.py)
- [tests/test_personality_modes.py](../../tests/test_personality_modes.py)
- [tests/test_phone_audio_relay.py](../../tests/test_phone_audio_relay.py)
- [tests/test_phone_calls.py](../../tests/test_phone_calls.py)
- [tests/test_phone_media_channels.py](../../tests/test_phone_media_channels.py)
- [tests/test_places.py](../../tests/test_places.py)
- [tests/test_player_panel_blink.py](../../tests/test_player_panel_blink.py)
- [tests/test_plugin_registry.py](../../tests/test_plugin_registry.py)
- [tests/test_prayer_times.py](../../tests/test_prayer_times.py)
- [tests/test_precision_stt.py](../../tests/test_precision_stt.py)
- [tests/test_precision_stt_pipeline.py](../../tests/test_precision_stt_pipeline.py)
- [tests/test_proactive_service.py](../../tests/test_proactive_service.py)
- [tests/test_prosody.py](../../tests/test_prosody.py)
- [tests/test_prosody_analyzer.py](../../tests/test_prosody_analyzer.py)
- [tests/test_radial_waveform.py](../../tests/test_radial_waveform.py)
- [tests/test_reliability_upgrade.py](../../tests/test_reliability_upgrade.py)
- [tests/test_reminder_parsing.py](../../tests/test_reminder_parsing.py)
- [tests/test_reminder_system.py](../../tests/test_reminder_system.py)
- [tests/test_rich_card_system.py](../../tests/test_rich_card_system.py)
- [tests/test_screen_capture.py](../../tests/test_screen_capture.py)
- [tests/test_screen_consciousness.py](../../tests/test_screen_consciousness.py)
- [tests/test_screen_reader.py](../../tests/test_screen_reader.py)
- [tests/test_scribe_stt.py](../../tests/test_scribe_stt.py)
- [tests/test_second_brain.py](../../tests/test_second_brain.py)
- [tests/test_self_healing.py](../../tests/test_self_healing.py)
- [tests/test_semantic_memory.py](../../tests/test_semantic_memory.py)
- [tests/test_send_message_args.py](../../tests/test_send_message_args.py)
- [tests/test_shell_exec_safety.py](../../tests/test_shell_exec_safety.py)
- [tests/test_shutdown_hooks.py](../../tests/test_shutdown_hooks.py)
- [tests/test_single_map.py](../../tests/test_single_map.py)
- [tests/test_smart_screen_pointer.py](../../tests/test_smart_screen_pointer.py)
- [tests/test_sparring_partner.py](../../tests/test_sparring_partner.py)
- [tests/test_spatial_audio.py](../../tests/test_spatial_audio.py)
- [tests/test_speaker_security.py](../../tests/test_speaker_security.py)
- [tests/test_speech_overlay.py](../../tests/test_speech_overlay.py)
- [tests/test_speech_text_sync.py](../../tests/test_speech_text_sync.py)
- [tests/test_stt_capture_fidelity.py](../../tests/test_stt_capture_fidelity.py)
- [tests/test_stt_endpointing.py](../../tests/test_stt_endpointing.py)
- [tests/test_subprocess_contract.py](../../tests/test_subprocess_contract.py)
- [tests/test_sys_metrics.py](../../tests/test_sys_metrics.py)
- [tests/test_text_clean.py](../../tests/test_text_clean.py)
- [tests/test_thought_streamer.py](../../tests/test_thought_streamer.py)
- [tests/test_thread_pool.py](../../tests/test_thread_pool.py)
- [tests/test_tiktok_coach.py](../../tests/test_tiktok_coach.py)
- [tests/test_tiktok_tracker.py](../../tests/test_tiktok_tracker.py)
- [tests/test_timers.py](../../tests/test_timers.py)
- [tests/test_tool_bridge.py](../../tests/test_tool_bridge.py)
- [tests/test_tool_packs.py](../../tests/test_tool_packs.py)
- [tests/test_tool_protections.py](../../tests/test_tool_protections.py)
- [tests/test_tool_registry.py](../../tests/test_tool_registry.py)
- [tests/test_turn_submit_lock.py](../../tests/test_turn_submit_lock.py)
- [tests/test_undo_stack.py](../../tests/test_undo_stack.py)
- [tests/test_vad_silero.py](../../tests/test_vad_silero.py)
- [tests/test_vector_memory_batching.py](../../tests/test_vector_memory_batching.py)
- [tests/test_video_hub.py](../../tests/test_video_hub.py)
- [tests/test_visual_pointer.py](../../tests/test_visual_pointer.py)
- [tests/test_voice_like.py](../../tests/test_voice_like.py)
- [tests/test_voice_selection.py](../../tests/test_voice_selection.py)
- [tests/test_vosk_stability.py](../../tests/test_vosk_stability.py)
- [tests/test_web_search_geo.py](../../tests/test_web_search_geo.py)
- [tests/test_web_search_hedge.py](../../tests/test_web_search_hedge.py)
- [tests/test_workspace_navigation_precision.py](../../tests/test_workspace_navigation_precision.py)
- [tests/test_youtube_chrome_routing.py](../../tests/test_youtube_chrome_routing.py)
- [tests/test_youtube_control.py](../../tests/test_youtube_control.py)
- [tests/test_zapzap_controller.py](../../tests/test_zapzap_controller.py)
## Éléments hors preuve d'exécution

Les bases personnelles n'ont pas été exportées ni réparées. Les secrets n'ont pas été recopiés. Pas de test d'envoi opérateur, Gmail, invitation agenda, modification de paquet Arch, exploitation réseau, crash natif volontaire de l'application en cours ou benchmark acoustique live. Les vérifications correspondantes sont explicitement planifiées dans la roadmap.

