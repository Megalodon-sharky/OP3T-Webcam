package com.op3t.webcam;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * Listens on TCP 8080. PC reaches it via: adb forward tcp:8080 tcp:8080.
 * Protocol: on connect the PC sends one text line "WIDTHxHEIGHT@FPS\n" (e.g. "1920x1080@60").
 * The app configures the encoder to that and streams raw H.264 back over the same socket.
 *
 * OLED power save: after the screen has been idle for a while during streaming, the screen is
 * painted pure black and brightness dropped to minimum (OLED black pixels draw ~no power).
 * The camera keeps streaming the whole time (held by a wake lock). Tap the screen to wake it.
 */
public class MainActivity extends AppCompatActivity {
    private static final String TAG = "OP3TWebcam";
    private static final int PORT = 8080;
    // Face rectangles ride a SEPARATE port. The 8080 phone->PC direction is a raw Annex-B byte
    // stream that the PC relays verbatim into ffmpeg's stdin, so injecting text there would corrupt
    // the elementary stream. A second port also fails independently: no PC listener, or a listener
    // that dies, changes nothing about the video.
    private static final int PORT_FACE = 8081;

    // Fallback if the PC sends no/å bad config line.
    private static final int DEF_W = 1920, DEF_H = 1080, DEF_FPS = 60;
    private static final int BITRATE_MBPS_1080 = 12;

    // Black the OLED this many ms after streaming starts / after the last tap.
    private static final long DIM_AFTER_MS = 30_000;
    private static final float BRIGHT_DIM = 0.01f;   // near-off; with black pixels OLED ~idle

    private TextView status;
    private ServerSocket server;
    private volatile CameraStreamer streamer;   // volatile: the face writer thread reads it
    private Thread acceptThread;
    private ServerSocket faceServer;
    private Thread faceAcceptThread;
    private PowerManager.WakeLock wakeLock;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable dimTask = this::dimScreen;
    private boolean dimmed = false;
    private volatile boolean streaming = false;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        status = new TextView(this);
        status.setTextSize(18);
        status.setTextColor(Color.WHITE);
        status.setPadding(40, 80, 40, 40);
        status.setBackgroundColor(Color.BLACK);
        status.setOnTouchListener((v, e) -> { if (e.getAction() == MotionEvent.ACTION_DOWN) wakeScreen(); return false; });
        setContentView(status);
        getWindow().getDecorView().setBackgroundColor(Color.BLACK);

        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "op3t:stream");

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, new String[]{Manifest.permission.CAMERA}, 1);
        } else {
            startServer();
        }
    }

    @Override public void onRequestPermissionsResult(int rc, @NonNull String[] p, @NonNull int[] r) {
        super.onRequestPermissionsResult(rc, p, r);
        if (r.length > 0 && r[0] == PackageManager.PERMISSION_GRANTED) startServer();
        else setStatus("Camera permission denied. Cannot stream.");
    }

    private void startServer() {
        setStatus("Waiting for PC on tcp:" + PORT + "\n\nOn PC: run the OP3T Webcam app (Start).");
        acceptThread = new Thread(() -> {
            try {
                server = new ServerSocket();
                server.setReuseAddress(true);                 // reclaim a TIME_WAIT/lingering bind from a prior run
                server.bind(new java.net.InetSocketAddress(PORT));
                while (!Thread.currentThread().isInterrupted()) {
                    Socket client = server.accept();          // blocks until PC connects
                    client.setTcpNoDelay(true);
                    handleClient(client);
                }
            } catch (Exception e) {
                Log.e(TAG, "server", e);
                runOnUiThread(() -> setStatus("Server error: " + e.getMessage()));
            }
        });
        acceptThread.start();
        startFaceServer();
    }

    /** Face-rectangle feed on tcp:8081. Its own accept thread, deliberately independent of the video
     *  path: it never touches `streamer`'s lifetime, never writes to socket 8080, and the video
     *  streams normally whether or not anything ever connects here. */
    private void startFaceServer() {
        faceAcceptThread = new Thread(() -> {
            try {
                faceServer = new ServerSocket();
                faceServer.setReuseAddress(true);
                faceServer.bind(new java.net.InetSocketAddress(PORT_FACE));
                while (!Thread.currentThread().isInterrupted()) {
                    Socket c = faceServer.accept();
                    c.setTcpNoDelay(true);
                    serveFaces(c);                    // one client at a time, like the video port
                }
            } catch (Exception e) {
                Log.e(TAG, "face server", e);         // never fatal: video does not depend on this
            }
        });
        faceAcceptThread.start();
    }

    /** Drains CameraStreamer's 1-deep face slot to one PC client. Drop-oldest by construction: the
     *  camera callback overwrites the slot, and we only ever send whatever is current when we look,
     *  so a slow PC makes us skip updates rather than build a backlog. */
    private void serveFaces(Socket c) {
        CameraStreamer sentHeaderFor = null;
        long seen = -1;
        try {
            java.io.OutputStream os = c.getOutputStream();
            while (!c.isClosed()) {
                CameraStreamer st = streamer;
                if (st == null || !st.isRunning()) {   // between sessions: wait, keep the socket up
                    sentHeaderFor = null;
                    Thread.sleep(200);
                    continue;
                }
                if (sentHeaderFor != st) {             // new session -> re-send the array header
                    String hdr = st.faceHeader();
                    if (hdr == null) { Thread.sleep(100); continue; }   // session not configured yet
                    os.write(hdr.getBytes());
                    os.flush();
                    sentHeaderFor = st;
                    seen = -1;
                }
                long seq = st.faceSequence();
                if (seq != seen) {
                    seen = seq;
                    String m = st.faceMessage();
                    if (m != null) { os.write(m.getBytes()); os.flush(); }
                }
                Thread.sleep(40);                      // 25 Hz poll against a 12 Hz publisher
            }
        } catch (Exception ignored) {                  // PC went away, or write failed -> drop it
        } finally {
            try { c.close(); } catch (Exception ignored) {}
        }
    }

    private void handleClient(Socket client) {
        int w = DEF_W, h = DEF_H, fps = DEF_FPS;
        try {
            // First line = "WxH@FPS". Read it byte-by-byte so we don't swallow following H.264 bytes.
            String line = readLine(client);
            int[] cfg = parseConfig(line);
            if (cfg != null) { w = cfg[0]; h = cfg[1]; fps = cfg[2]; }
            final int fw = w, fh = h, ff = fps;
            runOnUiThread(() -> { setStatus("PC connected. Streaming " + fw + "x" + fh + " @" + ff); armDim(); });

            if (!wakeLock.isHeld()) wakeLock.acquire();
            streaming = true;
            OutputStream os = client.getOutputStream();
            int bitrate = Math.max(4, (int) Math.round(BITRATE_MBPS_1080 * ((double) (w * h) / (1920 * 1080))));
            streamer = new CameraStreamer(this, w, h, fps, bitrate);
            streamer.start(os);
            // Same socket carries live control lines from the PC ("ZOOM 1.5", "FOCUS").
            client.setSoTimeout(500);                         // wake periodically to re-check isRunning
            while (streamer.isRunning()) {
                String cmd;
                try { cmd = readLine(client); }
                catch (java.net.SocketTimeoutException te) { continue; }
                if (cmd == null) break;                       // PC closed the connection
                if (!cmd.isEmpty()) handleControl(cmd);
            }
        } catch (Exception e) {
            Log.e(TAG, "client", e);
        } finally {
            streaming = false;
            if (streamer != null) streamer.stop();
            try { client.close(); } catch (Exception ignored) {}
            if (wakeLock.isHeld()) wakeLock.release();
            runOnUiThread(() -> { wakeScreen(); ui.removeCallbacks(dimTask); setStatus("PC disconnected. Waiting on tcp:" + PORT); });
        }
    }

    /** Reads one '\n'-terminated line (max 64 bytes) without buffering past it. null = EOF (PC closed). */
    private String readLine(Socket s) throws java.io.IOException {
        StringBuilder sb = new StringBuilder();
        java.io.InputStream in = s.getInputStream();
        for (int i = 0; i < 64; i++) {
            int c = in.read();
            if (c < 0) return (sb.length() == 0) ? null : sb.toString();   // EOF
            if (c == '\n') break;
            if (c != '\r') sb.append((char) c);
        }
        return sb.toString();
    }

    /** Live control from the PC over the same socket: "ZOOM <ratio>" or "FOCUS". */
    private void handleControl(String cmd) {
        try {
            if (cmd.startsWith("ZOOM ") && streamer != null) {
                String[] p = cmd.substring(5).trim().split("\\s+");   // "ratio [cx cy]"
                float r = Float.parseFloat(p[0]);
                if (p.length >= 3) streamer.setZoom(r, Float.parseFloat(p[1]), Float.parseFloat(p[2]));
                else streamer.setZoom(r);
            } else if (cmd.equals("FOCUS") && streamer != null)
                streamer.refocus();
            else if (cmd.startsWith("NR ") && streamer != null)
                streamer.setDenoise(cmd.substring(3).trim().equals("1"));   // phone-side low-light denoise
            else if (cmd.startsWith("FLASH ") && streamer != null)
                streamer.setTorch(cmd.substring(6).trim().equals("1"));     // rear LED torch
            else if (cmd.startsWith("EV ") && streamer != null)
                streamer.setEv(Integer.parseInt(cmd.substring(3).trim()));  // exposure compensation
            else if (cmd.startsWith("BITRATE ") && streamer != null)
                streamer.setBitrate(Integer.parseInt(cmd.substring(8).trim())); // live quality (Mbps)
            else if (cmd.startsWith("FOCUSDIST ") && streamer != null)          // manual focus slider
                streamer.setFocusDistance(Float.parseFloat(cmd.substring(10).trim())); // 0=auto..1=near
            else if (cmd.startsWith("XFORM ") && streamer != null) {       // phone-side GPU rotate+flip
                String[] p = cmd.substring(6).trim().split("\\s+");
                int r = p.length > 0 ? Integer.parseInt(p[0]) : 0;
                streamer.setTransform(r, p.length > 1 && p[1].equals("1"), p.length > 2 && p[2].equals("1"));
            }
        } catch (Exception e) { Log.e(TAG, "ctrl: " + cmd, e); }
    }

    /** "1920x1080@60" -> {1920,1080,60}; null if malformed. */
    private int[] parseConfig(String line) {
        try {
            String[] a = line.split("[x@]");
            if (a.length != 3) return null;
            return new int[]{Integer.parseInt(a[0].trim()), Integer.parseInt(a[1].trim()), Integer.parseInt(a[2].trim())};
        } catch (Exception e) { return null; }
    }

    // ---- OLED power save ----

    private void armDim() {
        ui.removeCallbacks(dimTask);
        ui.postDelayed(dimTask, DIM_AFTER_MS);
    }

    private void dimScreen() {
        if (!streaming) return;
        dimmed = true;
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = BRIGHT_DIM;
        getWindow().setAttributes(lp);
        status.setTextColor(Color.rgb(20, 20, 20));   // barely-visible hint on black -> OLED near-off
        status.setText("● streaming (screen dimmed to save power)\n\ntap to wake");
    }

    private void wakeScreen() {
        dimmed = false;
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE; // back to system brightness
        getWindow().setAttributes(lp);
        status.setTextColor(Color.WHITE);
        if (streaming) { setStatusKeepText(); armDim(); }
    }

    private void setStatusKeepText() {
        // refresh to the streaming line at full brightness, then re-arm dim
        if (streamer != null) status.setText("OP3T Webcam\n\nPC connected. Streaming (tap toggles wake).");
    }

    private void setStatus(String s) {
        if (dimmed) return;                 // don't blast the OLED bright while intentionally dimmed
        status.setText("OP3T Webcam\n\n" + s);
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        streaming = false;
        ui.removeCallbacks(dimTask);
        if (streamer != null) streamer.stop();
        try { if (server != null) server.close(); } catch (Exception ignored) {}
        try { if (faceServer != null) faceServer.close(); } catch (Exception ignored) {}
        if (acceptThread != null) acceptThread.interrupt();
        if (faceAcceptThread != null) faceAcceptThread.interrupt();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
    }
}
