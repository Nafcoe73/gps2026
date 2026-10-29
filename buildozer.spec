[app]
title = Mapa Offline
package.name = mapaoffline
package.domain = org.rota
source.dir = .
source.include_exts = py,png,txt,kv
version = 1.0

requirements = python3,kivy,plyer,pyjnius

orientation = portrait
fullscreen = 0

# GPS + leitura de ficheiros
android.permissions = ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION,READ_EXTERNAL_STORAGE

# mantém o ecrã ligado enquanto a app está aberta
android.wakelock = True

android.api = 34
android.minapi = 24
# Redmi Note 8 Pro (Helio G90T) = 64 bits
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
