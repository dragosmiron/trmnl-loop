Import("env")
import os
import subprocess

def merge_binaries(source, target, env):
    print("==================================================")
    print("Merging binaries into a single flashable firmware...")

    build_dir = env.subst("$BUILD_DIR")
    sdk_dir = env.PioPlatform().get_package_dir("framework-arduinoespressif32")
    project_dir = env.subst("$PROJECT_DIR")

    bootloader = os.path.join(build_dir, "bootloader.bin")
    partitions = os.path.join(build_dir, "partitions.bin")
    boot_app0 = os.path.join(sdk_dir, "tools", "partitions", "boot_app0.bin")
    app_bin = os.path.join(build_dir, "firmware.bin")
    
    esptool_dir = env.PioPlatform().get_package_dir("tool-esptoolpy")
    esptool_py = os.path.join(esptool_dir, "esptool.py")

    # Target output file
    output_bin = os.path.join(project_dir, "firmware.bin")

    # Construct the esptool merge_bin command
    # calling the specific esptool.py script packaged by PlatformIO
    cmd = [
        env.subst("$PYTHONEXE"), esptool_py,
        "--chip", "esp32s3",
        "merge_bin",
        "-o", output_bin,
        "--flash_mode", "dio",
        "--flash_size", "8MB",
        "0x0000", bootloader,
        "0x8000", partitions,
        "0xe000", boot_app0,
        "0x10000", app_bin
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"Successfully generated merged binary: {output_bin}")
    else:
        print("Failed to merge binaries!")
        env.Exit(1)
        
    print("==================================================")

# Register the post action to run when firmware.bin is built
env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", merge_binaries)
