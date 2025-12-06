from bandsox.core import BandSox

bs = BandSox(storage_dir="/home/rc/bandsox/storage")
vm = bs.restore_vm("arch-box", enable_networking=True)

vm.exec_command("echo 'Hello BandSox' > /root/test.txt")
def print_out(d): print(f"VM: {d.strip()}")
vm.exec_command("ip addr", on_stdout=print_out, on_stderr=print_out)
vm.exec_command("ip route", on_stdout=print_out, on_stderr=print_out)
exit_code = vm.exec_command("ping -c 3 8.8.8.8", on_stdout=print_out, on_stderr=print_out)
exit_code = vm.exec_command("ping -c 3 google.com", on_stdout=print_out, on_stderr=print_out)
print(vm.vm_id)
input()
vm.stop()
vm.delete()
import sys
if exit_code != 0:
    sys.exit(1)
with open("docker_stuff_success.txt", "w") as f:
    f.write("SUCCESS")
