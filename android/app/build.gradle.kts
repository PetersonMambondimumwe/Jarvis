plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jarvis.hud"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jarvis.hud"
        minSdk = 26
        targetSdk = 34
        versionCode = 49
        versionName = "48.2.0"
    }

    signingConfigs {
        create("release") {
            storeFile = file("jarvis-release.jks")
            storePassword = "jarvispassword123"
            keyAlias = "jarvis"
            keyPassword = "jarvispassword123"
            enableV1Signing = true
            enableV2Signing = true
            enableV3Signing = true
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            applicationIdSuffix = ""
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
}
