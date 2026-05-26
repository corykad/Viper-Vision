$ErrorActionPreference = "Stop"

python -m py_compile `
  main.pyw `
  viper_audio.py `
  viper_broadcast.py `
  viper_cinderella.py `
  viper_config.py `
  viper_discovery.py `
  viper_diagnostics.py `
  viper_ha_addons.py `
  viper_ha_listener.py `
  viper_ha_package.py `
  viper_ring_discovery.py `
  viper_speakers.py `
  viper_vacuum.py `
  viper_vision.py `
  tests\test_viper_release.py

python -m unittest discover -s tests -v
