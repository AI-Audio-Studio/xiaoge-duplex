/*
 * XiaogeWsAdapter —— 让 MATLAB 能用 Java-WebSocket 的适配器。
 *
 * 为什么需要它:MATLAB 无法直接子类化 Java 抽象类 WebSocketClient 并重写其
 * onOpen/onMessage/... 回调。本适配器替 MATLAB 完成子类化,并把事件通过
 * JavaBean 的 PropertyChangeSupport 派发出来——MATLAB 可用
 * set(obj,'PropertyChangeCallback',@fn) 接收(见 +xiaoge/Client.m)。
 *
 * 编译(需 JDK + Java-WebSocket jar,生成可被 MATLAB javaaddpath 的 jar):
 *   javac -cp lib/Java-WebSocket-<ver>.jar java/XiaogeWsAdapter.java -d build
 *   jar cf lib/XiaogeWsAdapter.jar -C build .
 * MATLAB 端:javaaddpath 同时加入这两个 jar。
 *
 * 事件 PropertyName:Open(Boolean) / Text(String) / Audio(byte[]) /
 *                   Closed(Boolean) / Error(String)。
 */
import java.beans.PropertyChangeListener;
import java.beans.PropertyChangeSupport;
import java.net.URI;
import java.nio.ByteBuffer;

import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

public class XiaogeWsAdapter extends WebSocketClient {
    private final PropertyChangeSupport pcs = new PropertyChangeSupport(this);

    public XiaogeWsAdapter(URI serverUri) {
        super(serverUri);
    }

    public void addPropertyChangeListener(PropertyChangeListener l) {
        pcs.addPropertyChangeListener(l);
    }

    public void removePropertyChangeListener(PropertyChangeListener l) {
        pcs.removePropertyChangeListener(l);
    }

    /** 发送一段上行 PCM(16k/单声道/int16 小端)。 */
    public void sendPcm(byte[] pcm) {
        send(pcm);
    }

    @Override
    public void onOpen(ServerHandshake handshake) {
        pcs.firePropertyChange("Open", null, Boolean.TRUE);
    }

    @Override
    public void onMessage(String message) {
        pcs.firePropertyChange("Text", null, message);
    }

    @Override
    public void onMessage(ByteBuffer bytes) {
        byte[] a = new byte[bytes.remaining()];
        bytes.get(a);
        pcs.firePropertyChange("Audio", null, a);
    }

    @Override
    public void onClose(int code, String reason, boolean remote) {
        pcs.firePropertyChange("Closed", null, Boolean.TRUE);
    }

    @Override
    public void onError(Exception ex) {
        pcs.firePropertyChange("Error", null, String.valueOf(ex.getMessage()));
    }
}
