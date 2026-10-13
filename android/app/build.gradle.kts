plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val galaxyUrl = (project.findProperty("galaxyUrl") as String?)?.trim().orEmpty()

android {
    namespace = "ai.galaxy.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "ai.galaxy.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "7.0.0"
        buildConfigField("String", "GALAXY_URL", "\"$galaxyUrl\"")
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
    implementation("androidx.appcompat:appcompat:1.7.0")
}
