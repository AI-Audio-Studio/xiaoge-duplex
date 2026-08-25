package com.xiaoge.client;

import org.junit.Assume;
import org.junit.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AndroidFileE2eTest {
    private static final int FRAME_BYTES = 640;

    @Test
    public void wavConnectsToR522FakeGatewayAndHandlesCommand() throws Exception {
        Assume.assumeTrue("set -Dxiaoge.e2e.enabled=true to run",
                Boolean.getBoolean("xiaoge.e2e.enabled"));

        String createSessionUrl = requireProperty("xiaoge.e2e.createSessionUrl");
        Path wavPath = Path.of(requireProperty("xiaoge.e2e.wavPath"));
        byte[] pcm = readPcm16kMono(wavPath);
        assertTrue("wav payload must not be empty", pcm.length > 0);

        CountDownLatch ready = new CountDownLatch(1);
        CountDownLatch cmd = new CountDownLatch(1);
        AtomicReference<Throwable> failure = new AtomicReference<>();
        AtomicReference<XiaogeClient> ref = new AtomicReference<>();

        XiaogeConfig cfg = new XiaogeConfig(
                createSessionUrl,
                "android-e2e-001",
                "{\"key_id\":\"dev-key\",\"signature\":\"hmac-signature\"}",
                Arrays.asList("audio", "text", "cmd", "state"),
                "xiaoge-android-e2e-r5.2.2",
                "{\"locale\":\"zh-CN\"}");

        XiaogeClient client = new XiaogeClient(cfg, new XiaogeClient.Listener() {
            @Override
            public void onReady(int sampleRate) {
                assertEquals(16000, sampleRate);
                ready.countDown();
            }

            @Override
            public void onCommand(CommandEvent event) {
                if ("cmd-g2-0001".equals(event.cmdId)) {
                    cmd.countDown();
                }
            }

            @Override
            public void onFailure(Throwable error) {
                failure.compareAndSet(null, error);
            }
        });
        ref.set(client);
        try {
            client.start();
            assertTrue("ctrl.ready timeout", ready.await(5, TimeUnit.SECONDS));
            client.sendFrontendState("hint", "awake", "speech", 1000);
            for (int offset = 0; offset < pcm.length; offset += FRAME_BYTES) {
                int end = Math.min(pcm.length, offset + FRAME_BYTES);
                client.sendPcm(Arrays.copyOfRange(pcm, offset, end));
            }
            assertTrue("data.cmd timeout", cmd.await(5, TimeUnit.SECONDS));
            assertTrue("sent wav bytes", pcm.length > 0);
            assertFalse("client failure: " + failure.get(), failure.get() != null);
            String result = "records=android-file-e2e sent=" + pcm.length
                    + " cmd=cmd-g2-0001 failures=0";
            String reportPath = System.getProperty("xiaoge.e2e.reportPath", "");
            if (!reportPath.isBlank()) {
                Files.write(Path.of(reportPath), (result + System.lineSeparator()).getBytes(StandardCharsets.UTF_8));
            }
            System.out.println(result);
        } finally {
            XiaogeClient c = ref.get();
            if (c != null) {
                c.close();
            }
        }
    }

    private static String requireProperty(String name) {
        String value = System.getProperty(name, "");
        if (value.isBlank()) {
            throw new IllegalArgumentException("missing system property: " + name);
        }
        return value;
    }

    private static byte[] readPcm16kMono(Path path) throws IOException {
        byte[] wav = Files.readAllBytes(path);
        if (wav.length <= 44
                || wav[0] != 'R'
                || wav[1] != 'I'
                || wav[2] != 'F'
                || wav[3] != 'F'
                || wav[8] != 'W'
                || wav[9] != 'A'
                || wav[10] != 'V'
                || wav[11] != 'E') {
            throw new IllegalArgumentException("expected WAV file: " + path);
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream(wav.length - 44);
        out.write(wav, 44, wav.length - 44);
        return out.toByteArray();
    }
}
