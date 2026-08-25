package com.xiaoge.client;

import android.content.Context;

import java.io.IOException;
import java.io.InputStream;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

import okhttp3.OkHttpClient;

public final class XiaogeTls {
    private XiaogeTls() {}

    public static OkHttpClient cloudClient(Context context) throws IOException, GeneralSecurityException {
        try (InputStream in = context.getResources().openRawResource(R.raw.xiaoge_cloud_ca)) {
            return fromPemCa(in);
        }
    }

    public static OkHttpClient fromPemCa(InputStream caPem) throws GeneralSecurityException {
        CertificateFactory certificateFactory = CertificateFactory.getInstance("X.509");
        Certificate ca = certificateFactory.generateCertificate(caPem);

        KeyStore keyStore = KeyStore.getInstance(KeyStore.getDefaultType());
        try {
            keyStore.load(null, null);
        } catch (IOException e) {
            throw new GeneralSecurityException("failed to initialize CA keystore", e);
        }
        keyStore.setCertificateEntry("xiaoge-cloud-ca", ca);

        TrustManagerFactory trustManagerFactory =
                TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        trustManagerFactory.init(keyStore);
        X509TrustManager trustManager = singleX509TrustManager(trustManagerFactory.getTrustManagers());

        SSLContext sslContext = SSLContext.getInstance("TLS");
        sslContext.init(null, new TrustManager[]{trustManager}, null);

        return new OkHttpClient.Builder()
                .sslSocketFactory(sslContext.getSocketFactory(), trustManager)
                .build();
    }

    public static OkHttpClient insecureClient() throws GeneralSecurityException {
        X509TrustManager trustAll = new X509TrustManager() {
            @Override
            public void checkClientTrusted(X509Certificate[] chain, String authType) {}

            @Override
            public void checkServerTrusted(X509Certificate[] chain, String authType) {}

            @Override
            public X509Certificate[] getAcceptedIssuers() {
                return new X509Certificate[0];
            }
        };

        SSLContext sslContext = SSLContext.getInstance("TLS");
        sslContext.init(null, new TrustManager[]{trustAll}, null);
        HostnameVerifier trustAnyHostname = (hostname, session) -> true;

        return new OkHttpClient.Builder()
                .sslSocketFactory(sslContext.getSocketFactory(), trustAll)
                .hostnameVerifier(trustAnyHostname)
                .build();
    }

    private static X509TrustManager singleX509TrustManager(TrustManager[] trustManagers)
            throws GeneralSecurityException {
        for (TrustManager trustManager : trustManagers) {
            if (trustManager instanceof X509TrustManager) {
                return (X509TrustManager) trustManager;
            }
        }
        throw new GeneralSecurityException("no X509TrustManager available");
    }
}
