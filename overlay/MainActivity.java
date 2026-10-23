package com.stanryden.tgwsproxy;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.net.Uri;
import android.os.Looper;
import android.os.PersistableBundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;
import com.chaquo.python.Python;
import java.io.File;
import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

public class MainActivity extends Activity {
    private TextView status;
    private TextView credsView;
    private Button addToTelegram;
    private Button copyPassword;
    private Switch verifySwitch;
    private volatile String tgLink;
    private volatile String proxyPassword;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable statusPoll = new Runnable() {
        @Override public void run() {
            if (status != null) status.setText("Статус: " + readableStatus(ProxyService.currentStatus));
            handler.postDelayed(this, 700);
        }
    };

    private String readableStatus(String value) {
        if (value == null || "stopped".equals(value)) return "остановлен";
        if ("starting".equals(value)) return "запускается…";
        if ("running".equals(value)) return "работает";
        if ("stopping".equals(value)) return "останавливается…";
        if (value.startsWith("error:")) return "ошибка — " + value.substring(6).trim();
        return value;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 10);
        }
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(36, 48, 36, 36);
        layout.setGravity(Gravity.TOP);
        scroll.addView(layout);
        TextView title = new TextView(this);
        title.setText("TG WS Proxy");
        title.setTextSize(28);
        layout.addView(title);
        TextView info = new TextView(this);
        info.setText("Локальный SOCKS5 для Telegram\nАдрес: 127.0.0.1\nПорт: 1080");
        info.setTextSize(17);
        info.setPadding(0, 24, 0, 12);
        layout.addView(info);
        credsView = new TextView(this);
        credsView.setText("Логин и пароль: загрузка…");
        credsView.setTextSize(16);
        credsView.setTextIsSelectable(true);
        credsView.setPadding(0, 0, 0, 12);
        layout.addView(credsView);
        status = new TextView(this);
        status.setText("Статус: остановлен");
        status.setTextSize(16);
        status.setPadding(0, 12, 0, 24);
        layout.addView(status);
        Button start = new Button(this);
        start.setText("Запустить прокси");
        start.setOnClickListener(v -> {
            Intent i = new Intent(this, ProxyService.class).setAction(ProxyService.ACTION_START);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i); else startService(i);
            status.setText("Статус: запускается…");
        });
        layout.addView(start);
        Button stop = new Button(this);
        stop.setText("Остановить прокси");
        stop.setOnClickListener(v -> {
            startService(new Intent(this, ProxyService.class).setAction(ProxyService.ACTION_STOP));
            status.setText("Статус: остановлен");
        });
        layout.addView(stop);
        addToTelegram = new Button(this);
        addToTelegram.setText("Добавить прокси в Telegram");
        addToTelegram.setEnabled(false);
        addToTelegram.setOnClickListener(v -> openInTelegram());
        layout.addView(addToTelegram);
        copyPassword = new Button(this);
        copyPassword.setText("Скопировать пароль");
        copyPassword.setEnabled(false);
        copyPassword.setOnClickListener(v -> copyPasswordToClipboard());
        layout.addView(copyPassword);
        verifySwitch = new Switch(this);
        verifySwitch.setText("Проверять TLS-сертификат (рекомендуется)");
        verifySwitch.setChecked(true);
        verifySwitch.setEnabled(false);
        verifySwitch.setPadding(0, 24, 0, 12);
        layout.addView(verifySwitch);
        Button shareLogs = new Button(this);
        shareLogs.setText("Поделиться логами");
        shareLogs.setOnClickListener(v -> confirmShareLogs());
        layout.addView(shareLogs);
        TextView note = new TextView(this);
        note.setText("Нажмите «Добавить прокси в Telegram» (или введите вручную SOCKS5 127.0.0.1:1080 с логином и паролем выше). Прокси доступен только с паролем: другие приложения на телефоне им воспользоваться не смогут. Оставьте уведомление службы активным.");
        note.setPadding(0, 28, 0, 0);
        layout.addView(note);
        setContentView(scroll);
        loadConnectionInfo();
    }

    private void loadConnectionInfo() {
        new Thread(() -> {
            String json = null;
            String error = null;
            try {
                json = Python.getInstance().getModule("android_bridge")
                    .callAttr("get_connection_info", 1080).toString();
            } catch (Throwable t) {
                error = t.toString();
            }
            final String fj = json;
            final String fe = error;
            handler.post(() -> onConnectionInfo(fj, fe));
        }, "conn-info").start();
    }

    private void onConnectionInfo(String json, String error) {
        if (json == null) {
            credsView.setText("Не удалось получить данные подключения: " + error);
            return;
        }
        try {
            JSONObject o = new JSONObject(json);
            tgLink = o.getString("link");
            proxyPassword = o.getString("password");
            credsView.setText("Логин: " + o.getString("user") + "\nПароль: " + proxyPassword);
            addToTelegram.setEnabled(true);
            copyPassword.setEnabled(true);
            verifySwitch.setOnCheckedChangeListener(null);
            verifySwitch.setChecked(o.optBoolean("verify_tls", true));
            verifySwitch.setOnCheckedChangeListener((button, checked) -> setVerifyTls(checked));
            verifySwitch.setEnabled(true);
        } catch (Exception e) {
            credsView.setText("Ошибка данных подключения: " + e);
        }
    }

    private void openInTelegram() {
        String link = tgLink;
        if (link == null) return;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(link)));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "Telegram не найден. Введите данные вручную.", Toast.LENGTH_LONG).show();
        }
    }

    private void copyPasswordToClipboard() {
        String password = proxyPassword;
        if (password == null) return;
        ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        if (cm == null) return;
        ClipData clip = ClipData.newPlainText("TG WS Proxy", password);
        if (Build.VERSION.SDK_INT >= 33) {
            PersistableBundle extras = new PersistableBundle();
            extras.putBoolean("android.content.extra.IS_SENSITIVE", true);
            clip.getDescription().setExtras(extras);
        }
        cm.setPrimaryClip(clip);
        Toast.makeText(this, "Пароль скопирован", Toast.LENGTH_SHORT).show();
    }

    private void setVerifyTls(boolean enabled) {
        new Thread(() -> {
            String error = null;
            try {
                Python.getInstance().getModule("android_bridge")
                    .callAttr("set_setting", "verify_tls", enabled);
            } catch (Throwable t) {
                error = t.toString();
            }
            final String fe = error;
            handler.post(() -> {
                String msg;
                if (fe != null) msg = "Не удалось сохранить настройку: " + fe;
                else if (enabled) msg = "Проверка сертификата включена. Применится после перезапуска прокси.";
                else msg = "ВНИМАНИЕ: проверка сертификата отключена (небезопасно). Применится после перезапуска прокси.";
                Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
            });
        }, "set-verify-tls").start();
    }

    private void confirmShareLogs() {
        new android.app.AlertDialog.Builder(this)
            .setTitle("Отправить лог?")
            .setMessage("Лог содержит IP-адреса и порты серверов Telegram, время и объём соединений. Пароль прокси и содержимое сообщений в лог не попадают. Выбранное приложение получит текст лога.")
            .setPositiveButton("Отправить", (d, w) -> shareLogs())
            .setNegativeButton("Отмена", null)
            .show();
    }


    private File findProxyLog(File root, int depth) {
        if (root == null || depth < 0 || !root.isDirectory()) return null;
        File direct = new File(root, "proxy.log");
        if (direct.isFile() && direct.length() > 0) return direct;
        File[] children = root.listFiles();
        if (children == null) return null;
        for (File child : children) {
            if (child.isDirectory()) {
                File found = findProxyLog(child, depth - 1);
                if (found != null) return found;
            }
        }
        return null;
    }

    private String readTail(File f, int maxBytes) throws java.io.IOException {
        try (java.io.RandomAccessFile raf = new java.io.RandomAccessFile(f, "r")) {
            long len = raf.length();
            long start = Math.max(0, len - maxBytes);
            raf.seek(start);
            byte[] buf = new byte[(int) (len - start)];
            raf.readFully(buf);
            String result = new String(buf, StandardCharsets.UTF_8);
            if (start > 0) {
                int nl = result.indexOf('\n');
                if (nl >= 0) result = result.substring(nl + 1);
                result = "[Показан только хвост лога]\n" + result;
            }
            return result;
        }
    }

    private void shareLogs() {
        File logFile = findProxyLog(getFilesDir(), 8);
        if (logFile == null || !logFile.isFile()) {
            new android.app.AlertDialog.Builder(this)
                .setTitle("Логи пока не найдены")
                .setMessage("Запусти прокси, подожди несколько секунд и повтори. Если файла всё ещё нет, пришли скриншот этого сообщения.")
                .setPositiveButton("ОК", null).show();
            return;
        }
        String contents;
        try {
            contents = readTail(logFile, 150_000);
        } catch (Exception e) {
            new android.app.AlertDialog.Builder(this)
                .setTitle("Не удалось прочитать лог")
                .setMessage(e.toString())
                .setPositiveButton("ОК", null).show();
            return;
        }
        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType("text/plain");
        send.putExtra(Intent.EXTRA_SUBJECT, "TG WS Proxy — proxy.log");
        send.putExtra(Intent.EXTRA_TEXT, "Файл: " + logFile.getAbsolutePath() + "\n\n" + contents);
        startActivity(Intent.createChooser(send, "Поделиться логами прокси"));
    }

    @Override protected void onResume() {
        super.onResume();
        handler.removeCallbacks(statusPoll);
        handler.post(statusPoll);
    }

    @Override protected void onPause() {
        handler.removeCallbacks(statusPoll);
        super.onPause();
    }
}
