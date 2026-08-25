# Clients R5.2.2 Partial Fresh E2E Summary

generated_at: 2026-08-10 17:13:52

fake Gateway: http://127.0.0.1:18082/create_session
wav: E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav

| Target | ExitCode | Log |
| --- | ---: | --- |
| python | 0 | E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\e2e\evidence\20260810_refresh_partial\python.log |
| matlab_bridge | 0 | E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\e2e\evidence\20260810_refresh_partial\matlab_bridge.log |
| android | 0 | E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\e2e\evidence\20260810_refresh_partial\android.log |
| c | not rerun to completion | C fresh rerun blocked by local build environment; see E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\e2e\evidence\20260810_171128\c.log |

Fresh partial e2e conclusion: Python, Android, and MATLAB bridge paths passed against the standalone fake Gateway. C e2e did not complete in the fresh rerun because the Android CMake build regenerated and failed to find CURL in the current local build environment.
