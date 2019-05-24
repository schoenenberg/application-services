#!/bin/sh

python3 ./tools/dependency_summary.py > ./DEPENDENCIES.md
python3 ./tools/dependency_summary.py --all-android-targets --package lockbox > megazords/lockbox/DEPENDENCIES.md
python3 ./tools/dependency_summary.py --all-android-targets --package fenix > megazords/fenix/DEPENDENCIES.md
python3 ./tools/dependency_summary.py --all-android-targets --package reference-browser > megazords/reference-browser/DEPENDENCIES.md
python3 ./tools/dependency_summary.py --all-ios-targets --package megazord_ios > megazords/ios/DEPENDENCIES.md
