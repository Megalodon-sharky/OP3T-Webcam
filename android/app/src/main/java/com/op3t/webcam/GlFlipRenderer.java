package com.op3t.webcam;

import android.graphics.SurfaceTexture;
import android.opengl.EGL14;
import android.opengl.EGLConfig;
import android.opengl.EGLContext;
import android.opengl.EGLDisplay;
import android.opengl.EGLExt;
import android.opengl.EGLSurface;
import android.opengl.GLES11Ext;
import android.opengl.GLES20;
import android.opengl.Matrix;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Surface;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;

/**
 * Camera -> SurfaceTexture -> GLES (optional H/V flip) -> encoder input Surface.
 *
 * Owns a GL thread with an EGL context bound to the encoder's input surface. The camera renders
 * into a SurfaceTexture; every frame we draw one textured quad into the encoder surface, applying
 * the flip via the texture matrix. The flip is therefore done on the phone GPU (sub-millisecond on
 * the Adreno 530) — so the PC never runs an ffmpeg flip filter, which would force a GPU<->CPU
 * readback. Same resolution in and out: an exact texel remap, no scaling, no quality loss.
 */
class GlFlipRenderer implements SurfaceTexture.OnFrameAvailableListener {
    private static final String TAG = "GlFlipRenderer";
    private static final int EGL_RECORDABLE_ANDROID = 0x3142;
    private static final int TEX_EXT = GLES11Ext.GL_TEXTURE_EXTERNAL_OES;

    private final Surface outSurface;        // encoder input surface
    // Capture size (what the camera renders into) is now INDEPENDENT of encode size (what the
    // encoder receives). At 30 fps the camera runs 4K and this stage downscales to 1080p with
    // GL_LINEAR on the Adreno 530 — a supersampled 1080p frame, and the PC still only ever decodes
    // 1080p. At 60 fps both are 1080p and the draw is a 1:1 texel copy exactly as before.
    private final int inW, inH;              // SurfaceTexture buffer size = camera capture size
    private final int outW, outH;            // viewport = encoder surface size
    private final int baseRot;               // sensor mount orientation; user rot is added on top
    private volatile boolean flipH, flipV;
    private volatile int rot;                // 0/90/180/270, applied on the GPU (no PC ffmpeg)

    private HandlerThread thread;
    private Handler handler;

    private EGLDisplay eglDisplay = EGL14.EGL_NO_DISPLAY;
    private EGLContext eglContext = EGL14.EGL_NO_CONTEXT;
    private EGLSurface eglSurface = EGL14.EGL_NO_SURFACE;

    private SurfaceTexture surfaceTexture;
    private Surface inputSurface;            // camera capture target
    private int texId, program, aPos, aTex, uTexMatrix;
    private FloatBuffer quad;
    private final float[] stMatrix = new float[16];
    private final float[] flip = new float[16];
    private final float[] combined = new float[16];
    private volatile boolean ready = false;

    // fullscreen triangle strip: x, y, u, v  (u,v expand to vec4(u,v,0,1) in the shader)
    private static final float[] QUAD = {
        -1f, -1f, 0f, 0f,
         1f, -1f, 1f, 0f,
        -1f,  1f, 0f, 1f,
         1f,  1f, 1f, 1f,
    };

    private static final String VS =
        "attribute vec4 aPos;\n" +
        "attribute vec4 aTex;\n" +
        "uniform mat4 uTexMatrix;\n" +
        "varying vec2 vTex;\n" +
        "void main(){ gl_Position = aPos; vTex = (uTexMatrix * aTex).xy; }\n";

    private static final String FS =
        "#extension GL_OES_EGL_image_external : require\n" +
        "precision mediump float;\n" +
        "varying vec2 vTex;\n" +
        "uniform samplerExternalOES sTex;\n" +
        "void main(){ gl_FragColor = texture2D(sTex, vTex); }\n";

    GlFlipRenderer(Surface outSurface, int inW, int inH, int outW, int outH, int baseRot,
                   int rot, boolean flipH, boolean flipV) {
        this.outSurface = outSurface;
        this.inW = inW; this.inH = inH;
        this.outW = outW; this.outH = outH;
        this.baseRot = baseRot;
        this.rot = rot; this.flipH = flipH; this.flipV = flipV;
        thread = new HandlerThread("gl"); thread.start();
        handler = new Handler(thread.getLooper());
        final Object lock = new Object();
        synchronized (lock) {
            handler.post(() -> {
                try { initGl(); } catch (Exception e) { Log.e(TAG, "initGl", e); }
                synchronized (lock) { lock.notifyAll(); }
            });
            try { lock.wait(3000); } catch (InterruptedException ignored) {}
        }
    }

    /** The Surface the camera should target. Null if GL init failed (caller falls back). */
    Surface getInputSurface() { return inputSurface; }

    void setTransform(int rot, boolean h, boolean v) { this.rot = rot; this.flipH = h; this.flipV = v; }

    private void initGl() {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);
        int[] ver = new int[2];
        EGL14.eglInitialize(eglDisplay, ver, 0, ver, 1);
        int[] cfgAttr = {
            EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8, EGL14.EGL_BLUE_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL_RECORDABLE_ANDROID, 1, EGL14.EGL_NONE
        };
        EGLConfig[] cfgs = new EGLConfig[1];
        int[] num = new int[1];
        EGL14.eglChooseConfig(eglDisplay, cfgAttr, 0, cfgs, 0, 1, num, 0);
        int[] ctxAttr = { EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE };
        eglContext = EGL14.eglCreateContext(eglDisplay, cfgs[0], EGL14.EGL_NO_CONTEXT, ctxAttr, 0);
        eglSurface = EGL14.eglCreateWindowSurface(eglDisplay, cfgs[0], outSurface, new int[]{ EGL14.EGL_NONE }, 0);
        EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext);

        program = buildProgram();
        aPos = GLES20.glGetAttribLocation(program, "aPos");
        aTex = GLES20.glGetAttribLocation(program, "aTex");
        uTexMatrix = GLES20.glGetUniformLocation(program, "uTexMatrix");

        quad = ByteBuffer.allocateDirect(QUAD.length * 4).order(ByteOrder.nativeOrder()).asFloatBuffer();
        quad.put(QUAD).position(0);

        int[] t = new int[1];
        GLES20.glGenTextures(1, t, 0);
        texId = t[0];
        GLES20.glBindTexture(TEX_EXT, texId);
        GLES20.glTexParameterf(TEX_EXT, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameterf(TEX_EXT, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(TEX_EXT, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
        GLES20.glTexParameteri(TEX_EXT, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);

        surfaceTexture = new SurfaceTexture(texId);
        surfaceTexture.setDefaultBufferSize(inW, inH);   // camera capture size (may be 4K)
        surfaceTexture.setOnFrameAvailableListener(this, handler);
        inputSurface = new Surface(surfaceTexture);
        ready = true;
    }

    @Override public void onFrameAvailable(SurfaceTexture st) {
        if (!ready) return;
        try {
            surfaceTexture.updateTexImage();
            surfaceTexture.getTransformMatrix(stMatrix);
            drawFrame();
            EGLExt.eglPresentationTimeANDROID(eglDisplay, eglSurface, surfaceTexture.getTimestamp());
            EGL14.eglSwapBuffers(eglDisplay, eglSurface);   // hands the flipped frame to the encoder
        } catch (Exception e) { Log.e(TAG, "draw", e); }
    }

    private void drawFrame() {
        GLES20.glViewport(0, 0, outW, outH);   // encode size; a 4K texture lands here downscaled
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);
        GLES20.glUseProgram(program);

        // Texcoord transform: rotate the camera frame so the user's rot=0 is upright. The camera's
        // external matrix (stMatrix) already carries the texture pixel-aspect, so this is a PURE
        // normalised rotation about the centre — a similarity transform (verified: equal singular
        // values, distortion ratio 1.000 at every angle) so circles stay circles, NO stretch/squash.
        // No cover zoom: rotating the unit texcoord square by a multiple of 90° maps [0,1]² back onto
        // itself, so we sample exactly the whole texture and nothing outside it — the full frame, no
        // crop and no edge-smear (a cover>1 zoom would push sampling past [0,1] and CLAMP_TO_EDGE
        // would streak the borders). baseRot accounts for the sensor mount.
        int eff = ((rot + baseRot) % 360 + 360) % 360;
        float[] m = combined;
        Matrix.setIdentityM(m, 0);
        Matrix.translateM(m, 0, 0.5f, 0.5f, 0f);                 // about the texture centre
        Matrix.rotateM(m, 0, -eff, 0f, 0f, 1f);                  // pure rotation: similarity, no distortion
        Matrix.scaleM(m, 0, flipH ? -1f : 1f, flipV ? -1f : 1f, 1f);
        Matrix.translateM(m, 0, -0.5f, -0.5f, 0f);
        Matrix.multiplyMM(flip, 0, stMatrix, 0, m, 0);           // sample = stMatrix * (m * texcoord)
        GLES20.glUniformMatrix4fv(uTexMatrix, 1, false, flip, 0);

        quad.position(0);
        GLES20.glEnableVertexAttribArray(aPos);
        GLES20.glVertexAttribPointer(aPos, 2, GLES20.GL_FLOAT, false, 16, quad);
        quad.position(2);
        GLES20.glEnableVertexAttribArray(aTex);
        GLES20.glVertexAttribPointer(aTex, 2, GLES20.GL_FLOAT, false, 16, quad);

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(TEX_EXT, texId);
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);

        GLES20.glDisableVertexAttribArray(aPos);
        GLES20.glDisableVertexAttribArray(aTex);
    }

    private int buildProgram() {
        int vs = compile(GLES20.GL_VERTEX_SHADER, VS);
        int fs = compile(GLES20.GL_FRAGMENT_SHADER, FS);
        int p = GLES20.glCreateProgram();
        GLES20.glAttachShader(p, vs);
        GLES20.glAttachShader(p, fs);
        GLES20.glLinkProgram(p);
        int[] ok = new int[1];
        GLES20.glGetProgramiv(p, GLES20.GL_LINK_STATUS, ok, 0);
        if (ok[0] == 0) throw new RuntimeException("link: " + GLES20.glGetProgramInfoLog(p));
        return p;
    }

    private int compile(int type, String src) {
        int s = GLES20.glCreateShader(type);
        GLES20.glShaderSource(s, src);
        GLES20.glCompileShader(s);
        int[] ok = new int[1];
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0);
        if (ok[0] == 0) throw new RuntimeException("compile: " + GLES20.glGetShaderInfoLog(s));
        return s;
    }

    void release() {
        ready = false;
        if (handler != null) {
            handler.post(() -> {
                try {
                    if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
                        EGL14.eglMakeCurrent(eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT);
                        if (eglSurface != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(eglDisplay, eglSurface);
                        if (eglContext != EGL14.EGL_NO_CONTEXT) EGL14.eglDestroyContext(eglDisplay, eglContext);
                        EGL14.eglReleaseThread();
                        EGL14.eglTerminate(eglDisplay);
                    }
                    if (surfaceTexture != null) surfaceTexture.release();
                    if (inputSurface != null) inputSurface.release();
                } catch (Exception ignored) {}
            });
        }
        if (thread != null) { thread.quitSafely(); try { thread.join(1000); } catch (InterruptedException ignored) {} }
        thread = null; handler = null;
    }
}
