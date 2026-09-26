package com.op3t.webcam;

import android.content.Context;
import android.graphics.Rect;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.Face;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Range;
import android.util.Size;
import android.view.Surface;

import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * Camera2 -> MediaCodec H.264 (Surface input) -> writes raw Annex-B NAL stream to an OutputStream.
 * No container, no framing. ffmpeg on the PC reads it as "-f h264".
 */
public class CameraStreamer {
    private static final String TAG = "CameraStreamer";

    private final Context ctx;
    private final int width, height, fps, bitrate;

    private CameraDevice camera;
    private CameraCaptureSession session;
    private MediaCodec encoder;
    private Surface encoderSurface;
    private HandlerThread camThread, encThread;
    private Handler camHandler, encHandler;
    private volatile boolean running;
    private OutputStream out;

    // live controls (zoom via sensor crop region; reused builder so we can re-issue the repeating request)
    private CaptureRequest.Builder reqBuilder;
    private Rect activeArray;
    private float maxZoom = 1f;
    private volatile float zoom = 1f;
    private volatile float zcx = 0.5f, zcy = 0.5f;   // zoom crop center (the PC "follow box")
    private volatile boolean denoise = false;        // phone-side ISP noise reduction for low light
    private volatile boolean torch = false;          // rear LED torch (PC "FLASH 1/0")
    private long outFrames = 0;                       // encoder output count, for the latency log
    private volatile float focus01 = -1f;             // -1 = autofocus; 0..1 = manual (PC "FOCUSDIST")
    private float minFocusDist = 0f;                  // LENS_INFO_MINIMUM_FOCUS_DISTANCE, diopters
    private volatile int ev = 0;                      // AE exposure-compensation steps (PC "EV <n>")
    private Range<Integer> evRange;                   // sensor's allowed EV-comp range; clamp to it
    private volatile int curBitrate;                 // live encoder bitrate (bps); survives restarts
    private volatile boolean flipH = false, flipV = false;
    private volatile int rot = 0;                    // 0/90/180/270, applied by the GL renderer
    private int sensorOrientation = 0;               // SENSOR_ORIENTATION; GL base rotation so rot=0 is upright
    private GlFlipRenderer glr;                       // camera->GL flip->encoder (null = direct path)
    private Surface captureTarget;                    // what the camera renders into
    private long facePubNs = 0;                       // rate-limit the face publish to ~12 Hz
    private int capW, capH;                           // capture (sensor readout) size, for the PC mapping
    private volatile String faceMsg;                  // newest face update; a WRITER thread drains this
    private volatile long faceSeq;                    // bumped on each publish so the writer skips repeats

    // 12 Hz. A human head does not move meaningfully faster, and the PC controller smooths over
    // 0.55-1.10 s anyway, so a higher rate would only burn camHandler time and USB bandwidth.
    private static final long FACE_PERIOD_NS = 1_000_000_000L / 12;

    /** Shared result callback. MUST be passed to EVERY setRepeatingRequest — the HAL only reports
     *  faces for requests that carry a callback, so a single site left on `null` makes face data
     *  stop dead the first time the user touches denoise/EV/flash/focus/zoom, with no error.
     *
     *  Runs on camHandler, the thread that services the camera: build a string and return. NO socket
     *  I/O here — a blocked write would stall capture itself. MainActivity's writer thread drains it.
     *
     *  Rects are published RAW, in active-array coordinates, together with the READ-BACK crop region
     *  (never the requested one — croppingType is CENTER_ONLY on this HAL, so the HAL rewrites what
     *  was asked for) and the capture size. All geometry is the PC's job, so a mapping fix is a
     *  Python edit rather than an APK rebuild. */
    private final CameraCaptureSession.CaptureCallback resultCb =
            new CameraCaptureSession.CaptureCallback() {
        @Override public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest r,
                                                 TotalCaptureResult result) {
            long now = System.nanoTime();
            if (now - facePubNs < FACE_PERIOD_NS) return;
            facePubNs = now;
            Face[] faces = result.get(CaptureResult.STATISTICS_FACES);
            Rect crop = result.get(CaptureResult.SCALER_CROP_REGION);
            if (crop == null) crop = activeArray;
            if (crop == null) return;
            Long ts = result.get(CaptureResult.SENSOR_TIMESTAMP);
            int n = (faces == null) ? 0 : faces.length;
            StringBuilder sb = new StringBuilder(64 + n * 32);
            sb.append("F ").append(ts == null ? 0 : ts / 1000L)
              .append(' ').append(crop.left).append(' ').append(crop.top)
              .append(' ').append(crop.width()).append(' ').append(crop.height())
              .append(' ').append(capW).append(' ').append(capH)
              .append(' ').append(n).append('\n');
            for (int i = 0; i < n; i++) {
                Rect b = faces[i].getBounds();
                sb.append(b.left).append(' ').append(b.top).append(' ')
                  .append(b.width()).append(' ').append(b.height()).append(' ')
                  .append(faces[i].getScore()).append('\n');
            }
            faceMsg = sb.toString();
            faceSeq++;                 // single writer (camHandler), single reader — no lock needed
        }
    };

    /** "A <arrLeft> <arrTop> <arrW> <arrH> <sensorOrientation>" — sent once when a PC connects to the
     *  face port. Null until the capture session has read the active array. */
    String faceHeader() {
        Rect a = activeArray;
        if (a == null) return null;
        return "A " + a.left + " " + a.top + " " + a.width() + " " + a.height()
                + " " + sensorOrientation + "\n";
    }

    String faceMessage() { return faceMsg; }
    long faceSequence() { return faceSeq; }

    public CameraStreamer(Context ctx, int width, int height, int fps, int bitrateMbps) {
        this.ctx = ctx;
        this.width = width;
        this.height = height;
        this.fps = fps;
        this.bitrate = bitrateMbps * 1_000_000;
        this.curBitrate = this.bitrate;
    }

    /** Picks the rear camera and the largest H.264-encodable size <= requested, returns chosen Size. */
    public Size chooseSize() throws CameraAccessException {
        CameraManager mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        String camId = rearCameraId(mgr);
        CameraCharacteristics cc = mgr.getCameraCharacteristics(camId);
        StreamConfigurationMap map = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size[] sizes = map.getOutputSizes(MediaCodec.class); // sizes the encoder Surface accepts
        Size best = null;
        for (Size s : sizes) {
            if (s.getWidth() <= width && s.getHeight() <= height) {
                if (best == null || (long) s.getWidth() * s.getHeight() > (long) best.getWidth() * best.getHeight())
                    best = s;
            }
        }
        if (best == null) best = Collections.max(Arrays.asList(sizes),
                (a, b) -> Long.compare((long) a.getWidth() * a.getHeight(), (long) b.getWidth() * b.getHeight()));
        return best;
    }

    /** Capture (sensor readout) size for the current frame rate. The ENCODER always runs at the
     *  requested output size — only the readout changes, and GlFlipRenderer downscales on the GPU.
     *  30 fps -> the largest same-aspect size up to 4K that the sensor can actually sustain at 30
     *  (supersampled 1080p: less noise, more real detail, and the PC still decodes only 1080p).
     *  60 fps -> the output size, because 4K readout cannot hold 60 on this sensor or encoder. */
    private Size chooseCaptureSize(Size out) throws CameraAccessException {
        if (fps > 30) return out;
        CameraManager mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        StreamConfigurationMap map = mgr.getCameraCharacteristics(rearCameraId(mgr))
                .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) return out;
        long maxDur = 1_000_000_000L / Math.max(1, fps);   // ns/frame the readout must keep up with
        Size best = out;
        for (Size s : map.getOutputSizes(android.graphics.SurfaceTexture.class)) {
            // same aspect ratio (cross-multiply avoids float rounding), bigger than what we have,
            // and no larger than 4K
            if (s.getWidth() * out.getHeight() != s.getHeight() * out.getWidth()) continue;
            if ((long) s.getWidth() * s.getHeight() <= (long) best.getWidth() * best.getHeight()) continue;
            if (s.getWidth() > 3840 || s.getHeight() > 2160) continue;
            try {
                if (map.getOutputMinFrameDuration(android.graphics.SurfaceTexture.class, s) > maxDur)
                    continue;                              // sensor cannot read this out fast enough
            } catch (Exception ignored) {}
            best = s;
        }
        return best;
    }

    private String rearCameraId(CameraManager mgr) throws CameraAccessException {
        for (String id : mgr.getCameraIdList()) {
            Integer f = mgr.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (f != null && f == CameraCharacteristics.LENS_FACING_BACK) return id;
        }
        return mgr.getCameraIdList()[0];
    }

    /** Sensor mount angle (0/90/180/270). The camera's external-texture matrix delivers the frame
     *  rotated by this; the GL renderer counter-rotates by it so the user's rot=0 comes out upright
     *  (matching the old direct camera->encoder path). */
    private int readSensorOrientation() {
        try {
            CameraManager mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
            Integer o = mgr.getCameraCharacteristics(rearCameraId(mgr))
                    .get(CameraCharacteristics.SENSOR_ORIENTATION);
            return o != null ? o : 0;
        } catch (Exception e) { Log.e(TAG, "sensorOrientation", e); return 0; }
    }

    public void start(OutputStream out) throws Exception {
        this.out = out;
        running = true;
        Size sz = chooseSize();
        Size cap = chooseCaptureSize(sz);          // may be 4K at 30 fps; encoder still runs at sz
        capW = cap.getWidth(); capH = cap.getHeight();   // the PC needs this to undo the FOV crop
        sensorOrientation = readSensorOrientation();
        Log.i(TAG, "Streaming at " + sz.getWidth() + "x" + sz.getHeight() + " @" + fps
                + " (capture " + cap.getWidth() + "x" + cap.getHeight() + ")"
                + " sensorOrientation=" + sensorOrientation);

        camThread = new HandlerThread("cam"); camThread.start();
        camHandler = new Handler(camThread.getLooper());
        encThread = new HandlerThread("enc"); encThread.start();
        encHandler = new Handler(encThread.getLooper());

        setupEncoder(sz.getWidth(), sz.getHeight());
        // GL flip stage between camera and encoder; direct fallback if GL init fails.
        try {
            glr = new GlFlipRenderer(encoderSurface, cap.getWidth(), cap.getHeight(),
                    sz.getWidth(), sz.getHeight(), sensorOrientation, rot, flipH, flipV);
            captureTarget = glr.getInputSurface();
        } catch (Throwable t) { Log.e(TAG, "gl init failed, using direct path", t); glr = null; }
        if (captureTarget == null) {
            if (glr != null) { glr.release(); glr = null; }
            captureTarget = encoderSurface;
        }
        openCamera(sz);
    }

    private void setupEncoder(int w, int h) throws Exception {
        MediaFormat fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h);
        fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        fmt.setInteger(MediaFormat.KEY_BIT_RATE, curBitrate);   // curBitrate carries a live quality change across restart
        fmt.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
        // GOP 5s (was 1s). Each PC connect builds a fresh encoder -> immediate IDR, so first-frame latency
        // is unaffected; this only makes the periodic full-IDR burst 5x rarer (less bandwidth/decode spike).
        // USB is reliable so mid-stream recovery within 5s is fine.
        fmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 5);
        // Low-latency encoder hints (drop end-to-end lag): realtime priority + tell the codec the live rate.
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0);              // 0 = realtime (API 23+)
        fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, fps);     // hint: encode at capture rate, no batching
        // KEY_LATENCY=1 (added API 26; OP3T is API 28): ask the encoder to emit an output frame after each
        // input frame instead of holding several in flight. This is the scrcpy-style "no encoder output
        // buffering" lever and targets the upstream latency that BOTH decoders showed equally.
        fmt.setInteger(MediaFormat.KEY_LATENCY, 1);
        // Baseline profile = NO B-frames -> no frame reordering -> lower end-to-end latency. The ~10%
        // efficiency loss vs High profile is irrelevant over a USB tether. Also pin max B-frames to 0.
        fmt.setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
        fmt.setInteger(MediaFormat.KEY_LEVEL,
                (width * height > 1920 * 1080) ? MediaCodecInfo.CodecProfileLevel.AVCLevel51
                                               : MediaCodecInfo.CodecProfileLevel.AVCLevel42);
        fmt.setInteger("max-bframes", 0);
        encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        try {
            encoder.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        } catch (Exception profileUnsupported) {
            // Some HALs reject an explicit profile/level — fall back to encoder defaults so we still stream.
            Log.w(TAG, "baseline profile rejected, using defaults", profileUnsupported);
            encoder.release();
            fmt.removeKey(MediaFormat.KEY_PROFILE);
            fmt.removeKey(MediaFormat.KEY_LEVEL);
            encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            encoder.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        }
        encoderSurface = encoder.createInputSurface();
        encoder.setCallback(new MediaCodec.Callback() {
            @Override public void onInputBufferAvailable(MediaCodec c, int i) {}
            @Override public void onOutputBufferAvailable(MediaCodec c, int idx, MediaCodec.BufferInfo info) {
                // Whole body guarded: during a fast restart the codec can be released between the
                // callback firing and us touching it -> getOutputBuffer/releaseOutputBuffer throw
                // IllegalStateException on this thread. Uncaught here = app crash. So swallow it.
                try {
                    if (!running) { c.releaseOutputBuffer(idx, false); return; }
                    ByteBuffer buf = c.getOutputBuffer(idx);
                    if (buf != null && info.size > 0) {
                        // PHONE-SIDE LATENCY, measured not guessed. presentationTimeUs is the
                        // camera/SurfaceTexture capture timestamp (CLOCK_MONOTONIC, same timebase as
                        // System.nanoTime), forwarded by eglPresentationTimeANDROID. So this delta is
                        // exactly capture -> GL -> encode -> about-to-write-to-socket. Logged once a
                        // second so it costs nothing; this is the number that was never measured.
                        if (++outFrames % Math.max(1, fps) == 0) {
                            long ageMs = (System.nanoTime() - info.presentationTimeUs * 1000L) / 1_000_000L;
                            Log.i(TAG, "capture->socket " + ageMs + " ms (frame " + outFrames + ")");
                        }
                        buf.position(info.offset);
                        buf.limit(info.offset + info.size);
                        byte[] data = new byte[info.size];
                        buf.get(data);
                        try { out.write(data); out.flush(); }
                        catch (Exception e) { running = false; } // PC disconnected -> stop
                    }
                    c.releaseOutputBuffer(idx, false);
                } catch (IllegalStateException stale) {
                    running = false;   // codec already gone (restart) — stop quietly, don't crash
                } catch (Exception ignored) {}
            }
            @Override public void onError(MediaCodec c, MediaCodec.CodecException e) { Log.e(TAG, "enc err", e); running = false; }
            @Override public void onOutputFormatChanged(MediaCodec c, MediaFormat f) {}
        }, encHandler);
        encoder.start();
    }

    @SuppressWarnings("MissingPermission") // caller guarantees CAMERA granted before start()
    private void openCamera(Size sz) throws CameraAccessException {
        CameraManager mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        String camId = rearCameraId(mgr);
        mgr.openCamera(camId, new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice cam) {
                camera = cam;
                try { createSession(); } catch (Exception e) { Log.e(TAG, "session", e); running = false; }
            }
            @Override public void onDisconnected(CameraDevice cam) { stop(); }
            @Override public void onError(CameraDevice cam, int err) { Log.e(TAG, "cam err " + err); stop(); }
        }, camHandler);
    }

    private void createSession() throws CameraAccessException {
        CameraManager mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        CameraCharacteristics cc = mgr.getCameraCharacteristics(rearCameraId(mgr));
        activeArray = cc.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE);
        Float mz = cc.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM);
        maxZoom = (mz != null) ? mz : 1f;
        evRange = cc.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE);   // EV-comp steps allowed
        // Closest focus in diopters (1/metres). 0 means a fixed-focus lens, in which case the manual
        // focus slider has nothing to drive and we stay on autofocus.
        Float mfd = cc.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE);
        minFocusDist = (mfd != null) ? mfd : 0f;

        List<Surface> outputs = Collections.singletonList(captureTarget);
        camera.createCaptureSession(outputs, new CameraCaptureSession.StateCallback() {
            @Override public void onConfigured(CameraCaptureSession s) {
                session = s;
                try {
                    CaptureRequest.Builder b = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                    b.addTarget(captureTarget);
                    // Lock focus once (no continuous hunting -> no jitter); EIS off cuts latency + wobble.
                    b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_AUTO);
                    b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE,
                            CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_OFF);
                    reqBuilder = b;
                    applyImageTuning(b);                 // AE fps range + noise reduction (low-light aware)
                    s.setRepeatingRequest(b.build(), resultCb, camHandler);
                    // Diagnostic: read back the actual NR mode the HAL reported for the initial request.
                    s.capture(b.build(), new CameraCaptureSession.CaptureCallback() {
                        @Override public void onCaptureCompleted(CameraCaptureSession ss, CaptureRequest r, TotalCaptureResult result) {
                            Integer nrMode = result.get(CaptureResult.NOISE_REDUCTION_MODE);
                            Log.i(TAG, "session started with denoise=" + denoise + " actual NR mode=" + nrMode);
                        }
                    }, camHandler);
                    // fire a single autofocus, then leave the lens parked there
                    b.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_START);
                    s.capture(b.build(), null, camHandler);
                    b.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_IDLE);
                    if (zoom != 1f) setZoom(zoom);   // honor a zoom set before the session was ready
                } catch (Exception e) { Log.e(TAG, "repeat", e); running = false; }
            }
            @Override public void onConfigureFailed(CameraCaptureSession s) { Log.e(TAG, "session config failed"); stop(); }
        }, camHandler);
    }

    /** Noise reduction + auto-exposure range. Low-light noise on this old sensor is ISO/gain noise,
     *  so the dominant lever isn't the NR mode (FAST vs HIGH_QUALITY is near-invisible on a live
     *  stream) but EXPOSURE: in denoise mode we let AE drop the frame rate hard (down to ~7fps) so it
     *  uses a much longer exposure -> lower analog gain -> far less sensor noise, then HIGH_QUALITY
     *  ISP denoise cleans the rest. Off = pinned fps for lowest latency + cheap FAST denoise. */
    private void applyImageTuning(CaptureRequest.Builder b) {
        // HAL face detection for PC-side auto-framing. Set HERE, not at session start, because this
        // method is re-invoked on every request rebuild (denoise/EV/flash/focus/zoom) and anything
        // set only once would be silently dropped by the next rebuild. SIMPLE is the only mode this
        // device offers (measured: availableFaceDetectModes = [OFF, SIMPLE], maxFaceCount = 10).
        b.set(CaptureRequest.STATISTICS_FACE_DETECT_MODE,
              CaptureRequest.STATISTICS_FACE_DETECT_MODE_SIMPLE);
        if (denoise) {
            int lo = Math.min(7, fps);               // big AE headroom -> long exposure -> low ISO -> less noise
            b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, new Range<>(lo, fps));
            b.set(CaptureRequest.NOISE_REDUCTION_MODE, CaptureRequest.NOISE_REDUCTION_MODE_HIGH_QUALITY);
            b.set(CaptureRequest.EDGE_MODE, CaptureRequest.EDGE_MODE_HIGH_QUALITY);
            b.set(CaptureRequest.TONEMAP_MODE, CaptureRequest.TONEMAP_MODE_HIGH_QUALITY);
        } else {
            b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, new Range<>(fps, fps));
            b.set(CaptureRequest.NOISE_REDUCTION_MODE, CaptureRequest.NOISE_REDUCTION_MODE_FAST);
            b.set(CaptureRequest.EDGE_MODE, CaptureRequest.EDGE_MODE_FAST);
        }
        // Focus rides along every rebuild too, or a denoise/EV/zoom re-issue would silently drop the
        // manual lens position. Manual focus means AF fully OFF — leaving AF_MODE_AUTO on would let
        // the HAL move the lens back on the next trigger.
        if (focus01 >= 0f && minFocusDist > 0f) {
            b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_OFF);
            b.set(CaptureRequest.LENS_FOCUS_DISTANCE, focus01 * minFocusDist);
        } else {
            b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_AUTO);
        }
        // EV-comp + torch ride along every request rebuild so they survive a denoise/zoom re-issue.
        int e = ev;
        if (evRange != null) e = Math.max(evRange.getLower(), Math.min(e, evRange.getUpper()));
        b.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, e);
        b.set(CaptureRequest.FLASH_MODE,
                torch ? CaptureRequest.FLASH_MODE_TORCH : CaptureRequest.FLASH_MODE_OFF);
    }

    /** Toggle low-light noise reduction live (PC sends "NR 1" / "NR 0"). */
    public void setDenoise(boolean on) {
        this.denoise = on;
        if (session == null || reqBuilder == null) return;
        try {
            applyImageTuning(reqBuilder);
            session.setRepeatingRequest(reqBuilder.build(), resultCb, camHandler);
            // Diagnostic: fire a one-shot capture to read back the actual mode the HAL applied.
            session.capture(reqBuilder.build(), new CameraCaptureSession.CaptureCallback() {
                @Override public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest r, TotalCaptureResult result) {
                    Integer nrMode = result.get(CaptureResult.NOISE_REDUCTION_MODE);
                    Integer edgeMode = result.get(CaptureResult.EDGE_MODE);
                    Log.i(TAG, "setDenoise(" + on + ") actual NR mode=" + nrMode + " edge=" + edgeMode);
                }
            }, camHandler);
        } catch (Exception e) { Log.e(TAG, "denoise", e); }
    }

    /** Re-issue the repeating request after a live AE/torch change (EV, flash). No restart. */
    private void reapply() {
        if (session == null || reqBuilder == null) return;
        try {
            applyImageTuning(reqBuilder);
            session.setRepeatingRequest(reqBuilder.build(), resultCb, camHandler);
        } catch (Exception e) { Log.e(TAG, "reapply", e); }
    }

    /** Rear LED torch on/off (PC "FLASH 1/0"). */
    public void setTorch(boolean on) { this.torch = on; reapply(); }

    /** Exposure compensation in AE steps; clamped to the sensor range (PC "EV <n>"). */
    public void setEv(int steps) { this.ev = steps; reapply(); }

    /** Live encoder bitrate in Mbps — the "quality" knob. Lower = lighter USB + lighter PC decode
     *  (a lag lever when the GPU/CPU is busy). Applied to the running encoder, no restart. */
    public void setBitrate(int mbps) {
        int bps = Math.max(1, mbps) * 1_000_000;
        this.curBitrate = bps;
        if (encoder == null) return;
        try {
            android.os.Bundle p = new android.os.Bundle();
            p.putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps);
            encoder.setParameters(p);
        } catch (Exception e) { Log.e(TAG, "bitrate", e); }
    }

    /** Live rotation + H/V flip on the phone GPU (PC sends "XFORM <rot> <h> <v>"). No restart. */
    public void setTransform(int rot, boolean h, boolean v) {
        this.rot = rot; this.flipH = h; this.flipV = v;
        if (glr != null) glr.setTransform(rot, h, v);
    }

    public void setZoom(float ratio) { setZoom(ratio, zcx, zcy); }

    /** Real-time digital zoom (1.0 = none) panned to center (cx,cy) in 0..1 — the PC "follow box". */
    public void setZoom(float ratio, float cx, float cy) {
        ratio = Math.max(1f, Math.min(ratio, maxZoom));
        this.zoom = ratio; this.zcx = cx; this.zcy = cy;
        if (session == null || reqBuilder == null || activeArray == null) return; // applied when session ready
        int aw = activeArray.width(), ah = activeArray.height();
        int cw = Math.round(aw / ratio), ch = Math.round(ah / ratio);
        int left = activeArray.left + Math.round(cx * aw - cw / 2f);
        int top = activeArray.top + Math.round(cy * ah - ch / 2f);
        left = Math.max(activeArray.left, Math.min(left, activeArray.left + aw - cw));  // keep crop in bounds
        top = Math.max(activeArray.top, Math.min(top, activeArray.top + ah - ch));
        reqBuilder.set(CaptureRequest.SCALER_CROP_REGION, new Rect(left, top, left + cw, top + ch));
        try { session.setRepeatingRequest(reqBuilder.build(), resultCb, camHandler); }
        catch (Exception e) { Log.e(TAG, "zoom", e); }
    }

    /** Manual focus from the PC ("FOCUSDIST <0..1>"): 0 = hand control back to autofocus,
     *  1 = the lens's closest focus. Live, no restart. */
    public void setFocusDistance(float f01) {
        if (f01 <= 0f) { refocus(); return; }          // 0 on the slider means "go back to auto"
        focus01 = Math.min(1f, f01);
        reapply();
    }

    /** Re-run a one-shot autofocus (e.g. subject distance changed), and leave manual focus. */
    public void refocus() {
        focus01 = -1f;             // a pinned manual lens would ignore the AF trigger entirely
        if (session == null || reqBuilder == null) return;
        try {
            applyImageTuning(reqBuilder);              // restores CONTROL_AF_MODE_AUTO
            session.setRepeatingRequest(reqBuilder.build(), resultCb, camHandler);
            reqBuilder.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_START);
            session.capture(reqBuilder.build(), null, camHandler);
            reqBuilder.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_IDLE);
        } catch (Exception e) { Log.e(TAG, "refocus", e); }
    }

    public float getMaxZoom() { return maxZoom; }

    /** Fully release camera + encoder and BLOCK until the threads are gone, so the next start()
     *  can reopen the camera without hitting CAMERA_IN_USE (the rapid-restart crash). Idempotent. */
    public synchronized void stop() {
        running = false;
        try { if (glr != null) glr.release(); } catch (Exception ignored) {}
        glr = null; captureTarget = null;
        try { if (session != null) session.close(); } catch (Exception ignored) {}
        try { if (camera != null) camera.close(); } catch (Exception ignored) {}     // blocks until released
        try { if (encoder != null) { encoder.stop(); encoder.release(); } } catch (Exception ignored) {}
        if (camThread != null) { camThread.quitSafely(); try { camThread.join(1000); } catch (InterruptedException ignored) {} }
        if (encThread != null) { encThread.quitSafely(); try { encThread.join(1000); } catch (InterruptedException ignored) {} }
        session = null; camera = null; encoder = null; reqBuilder = null;
        camThread = null; encThread = null;
    }

    public boolean isRunning() { return running; }
}
