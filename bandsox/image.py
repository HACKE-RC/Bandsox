import subprocess
import os
import logging
import tempfile
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

def run_command(cmd, check=True, capture_output=True):
    """Helper to run shell commands."""
    logger.debug(f"Running command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e.cmd}")
        logger.error(f"Stdout: {e.stdout}")
        logger.error(f"Stderr: {e.stderr}")
        raise e

    logger.info(f"Rootfs created at {output_path}")
    return str(output_path)

def build_rootfs(docker_image: str, output_path: str, size_mb: int = 4096):
    """
    Converts a Docker image to a bootable rootfs ext4 image using fakeroot.
    """
    import docker
    client = docker.from_env()
    
    output_path = Path(output_path).resolve()
    if output_path.exists():
        logger.warning(f"Output path {output_path} exists, overwriting.")
        output_path.unlink()

    # 1. Pull/Verify Docker Image
    logger.info(f"Pulling/Verifying docker image: {docker_image}")
    try:
        client.images.pull(docker_image)
    except docker.errors.APIError:
        logger.warning(f"Failed to pull {docker_image}, checking if it exists locally...")
        try:
            client.images.get(docker_image)
            logger.info(f"Image {docker_image} found locally.")
        except docker.errors.ImageNotFound:
            logger.error(f"Image {docker_image} not found locally or remotely.")
            raise

    # 2. Create a container
    container = client.containers.create(docker_image)
    container_id = container.id
    logger.info(f"Created temporary container: {container_id}")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            tar_path = Path(temp_dir) / "rootfs.tar"
            extract_dir = Path(temp_dir) / "rootfs"
            extract_dir.mkdir()
            
            # 3. Export container filesystem
            logger.info("Exporting container filesystem...")
            with open(tar_path, "wb") as f:
                for chunk in container.export():
                    f.write(chunk)
            
            # 4. Create ext4 image using fakeroot
            # We use a single fakeroot session to extract and build fs to preserve permissions
            logger.info(f"Creating ext4 image at {output_path} using fakeroot...")
            
            # Prepare the command script
            # We need to add resolv.conf and potentially init script if missing
            # But we can't easily edit files inside fakeroot context unless we do it inside the script.
            
            # Script to run under fakeroot:
            # 1. tar -xf rootfs.tar -C rootfs_dir
            # 2. echo "nameserver 8.8.8.8" > rootfs_dir/etc/resolv.conf
            # 3. Overwrite inittab
            # 4. mkfs.ext4 -d rootfs_dir output.ext4
            
            # Create empty file first
            run_command(["dd", "if=/dev/zero", f"of={output_path}", "bs=1M", f"count={size_mb}"])
            run_command(["mkfs.ext4", str(output_path)]) 

            script = f"""
            set -e
            tar -xf {tar_path} -C {extract_dir}
            mkdir -p {extract_dir}/etc
            echo 'nameserver 8.8.8.8' > {extract_dir}/etc/resolv.conf
            
            # Create /init script
            # Use 'EOF' to prevent variable expansion in the HEREDOC
            cat <<'EOF' > {extract_dir}/init
#!/bin/sh
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
# Search for python3 in common locations
P=""
for path in /usr/local/bin/python3 /usr/bin/python3 /usr/local/bin/python /usr/bin/python; do
  if [ -x "$path" ]; then
    P="$path"
    break
  fi
done

if [ -z "$P" ]; then
  # Try to find via command/which as fallback
  P=$(which python3 2>/dev/null || which python 2>/dev/null)
fi

# Fallback: check for existence if executable check failed (e.g. some filesystems)
if [ -z "$P" ]; then
  if [ -f "/usr/bin/python3" ]; then
    P="/usr/bin/python3"
  elif [ -f "/usr/bin/python" ]; then
    P="/usr/bin/python"
  fi
fi

if [ -z "$P" ]; then
  echo "Warning: Python 3 not detected. Attempting to run agent directly (relying on shebang)..."
  exec /usr/local/bin/agent.py 2>&1
else
  exec $P /usr/local/bin/agent.py 2>&1
fi
EOF
            chmod +x {extract_dir}/init

            # Populate the filesystem
            mkfs.ext4 -O ^metadata_csum,^64bit -F -d {extract_dir} {output_path}
            """
            
            # We need to copy agent.py into the rootfs BEFORE mkfs
            # But we are outside fakeroot here.
            # We can copy it to extract_dir.
            agent_src = Path(__file__).parent / "agent.py"
            agent_dst = extract_dir / "usr/local/bin/agent.py"
            
            # We need to make sure /usr/local/bin exists
            (extract_dir / "usr/local/bin").mkdir(parents=True, exist_ok=True)
            shutil.copy2(agent_src, agent_dst)
            
            # Make it executable
            agent_dst.chmod(0o755)
            
            run_command(["fakeroot", "sh", "-c", script])

    except Exception:
        if output_path.exists():
            output_path.unlink()
        raise

    finally:
        logger.info(f"Removing container {container_id}")
        container.remove()

    logger.info(f"Rootfs created at {output_path}")
    return str(output_path)

def build_image_from_dockerfile(dockerfile_path: str, tag: str, nocache: bool = False):
    """Builds a Docker image from a Dockerfile using Docker SDK."""
    import docker
    client = docker.from_env()
    
    dockerfile_path = Path(dockerfile_path).resolve()
    if not dockerfile_path.exists():
        raise FileNotFoundError(f"Dockerfile not found at {dockerfile_path}")
    
    logger.info(f"Building Docker image {tag} from {dockerfile_path}")
    
    # docker-py build expects 'path' to directory containing Dockerfile
    # and 'dockerfile' arg if filename is not Dockerfile
    if dockerfile_path.is_file():
        path = str(dockerfile_path.parent)
        dockerfile = dockerfile_path.name
        client.images.build(path=path, dockerfile=dockerfile, tag=tag, rm=True, nocache=nocache)
    else:
        path = str(dockerfile_path)
        client.images.build(path=path, tag=tag, rm=True, nocache=nocache)
        
    return tag
