/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.jetbrains.kotlin.android)
  alias(libs.plugins.compose.compiler)
}

android {
  namespace = "com.meta.wearable.dat.externalsampleapps.cameraaccess"
  compileSdk = 36

  buildFeatures { buildConfig = true }

  defaultConfig {
    applicationId = "com.meta.wearable.dat.externalsampleapps.cameraaccess"
    minSdk = 31
    targetSdk = 36
    versionCode = 1
    versionName = "1.0"

    testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

    // Meta Wearables Device Access Toolkit Setup
    // Without Developer Mode, these values need to be set with credentials from the app registered
    // in Wearables Developer Center
    manifestPlaceholders["mwdat_application_id"] = "0"   // Developer Mode: the docs require "0"; an empty string fails to parse
    manifestPlaceholders["mwdat_client_token"] = "0"

    // Wake word language: ./gradlew assembleDebug -Psmotri.wake=ru  (default: en)
    // en → "hey look" with vosk-model-small-en-us; ru → "smotri" with vosk-model-small-ru.
    // ("hey luke" is how the small English model often hears "hey look".)
    val wake = (project.findProperty("smotri.wake") as String?) ?: "en"
    val (wakeWords, wakeHint) = if (wake == "ru") "смотри" to "«Смотри»" else "hey look|hey luke" to "\"Hey look\""
    buildConfigField("String", "WAKE_WORDS", "\"$wakeWords\"")
    buildConfigField("String", "WAKE_HINT", "\"${wakeHint.replace("\"", "\\\"")}\"")
    buildConfigField("String", "WAKE_MODEL_ASSET", "\"vosk-model-$wake\"")
  }

  buildTypes {
    release {
      isMinifyEnabled = true
      proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
      signingConfig = signingConfigs.getByName("debug")
    }
  }
  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  packaging { resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" } }
  signingConfigs {
    getByName("debug") {
      storeFile = file("sample.keystore")
      storePassword = "sample"
      keyAlias = "sample"
      keyPassword = "sample"
    }
  }
}

kotlin { compilerOptions { jvmTarget = JvmTarget.JVM_17 } }

dependencies {
  implementation(libs.androidx.activity.compose)
  implementation(platform(libs.androidx.compose.bom))
  implementation(libs.androidx.exifinterface)
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)
  implementation(libs.androidx.material.icons.extended)
  implementation(libs.androidx.material3)
  implementation(libs.kotlinx.collections.immutable)
  implementation(libs.mwdat.core)
  implementation(libs.mwdat.camera)
  implementation(libs.mwdat.mockdevice)
  // Smotri: WebSocket client for the server
  implementation("com.squareup.okhttp3:okhttp:4.12.0")
  // offline wake word (the model is put into assets by the CI build)
  implementation("com.alphacephei:vosk-android:0.3.47")
  // media: ExoPlayer (media3) for radio/track streams by URL
  implementation("androidx.media3:media3-exoplayer:1.4.1")
  androidTestImplementation(libs.androidx.ui.test.junit4)
  androidTestImplementation(libs.androidx.test.uiautomator)
  androidTestImplementation(libs.androidx.test.rules)
}
