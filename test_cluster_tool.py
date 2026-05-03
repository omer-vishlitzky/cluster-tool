# test_cluster_tool.py
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest

loader = importlib.machinery.SourceFileLoader("cluster_tool", "./cluster-tool")
spec = importlib.util.spec_from_loader("cluster_tool", loader, origin="./cluster-tool")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "state.json")
        ct.LOCAL_STATE_FILE = type(ct.LOCAL_STATE_FILE)(self.state_file)
        ct.LOCAL_STATE_DIR = type(ct.LOCAL_STATE_DIR)(self.tmpdir)

    def test_load_empty_state(self):
        state = ct.load_state()
        self.assertIsNone(state["snapshot"])
        self.assertEqual(state["clones"], {})
        self.assertEqual(state["next_subnet"], 160)

    def test_save_and_load_roundtrip(self):
        state = {"snapshot": {"source": "abc"}, "clones": {}, "next_subnet": 162}
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertEqual(loaded, state)

    def test_allocate_subnet_increments(self):
        state = ct.load_state()
        s1 = ct.allocate_subnet(state)
        s2 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)
        self.assertEqual(s2, 161)
        self.assertEqual(state["next_subnet"], 162)


class TestIDGeneration(unittest.TestCase):
    def test_clone_id_format(self):
        cid = ct.generate_clone_id()
        self.assertEqual(len(cid), 8)
        int(cid, 16)  # must be valid hex

    def test_clone_ids_are_unique(self):
        ids = {ct.generate_clone_id() for _ in range(100)}
        self.assertGreater(len(ids), 90)

    def test_mac_format(self):
        mac = ct.generate_mac()
        parts = mac.split(":")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "02")
        self.assertEqual(parts[1], "00")
        self.assertEqual(parts[2], "00")


class TestTemplates(unittest.TestCase):
    def test_primary_network_xml(self):
        xml = ct.gen_primary_network_xml("a1b2c3d4", 160, "02:00:00:aa:bb:cc")
        self.assertIn("<name>test-infra-net-a1b2c3d4</name>", xml)
        self.assertIn("192.168.160.1", xml)
        self.assertIn("192.168.160.10", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("api.test-infra-cluster-a1b2c3d4.redhat.com", xml)
        self.assertIn("192.168.160.100", xml)
        self.assertIn("192.168.160.101", xml)

    def test_secondary_network_xml(self):
        xml = ct.gen_secondary_network_xml("a1b2c3d4", 160, 178, "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-secondary-network-a1b2c3d4</name>", xml)
        self.assertIn("192.168.178.1", xml)
        self.assertIn("192.168.178.10", xml)
        self.assertIn("192.168.160.100", xml)  # DNS points to primary VIP

    def test_vm_xml(self):
        xml = ct.gen_vm_xml("a1b2c3d4", "/path/overlay.qcow2", "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-cluster-a1b2c3d4-master-0</name>", xml)
        self.assertIn("/path/overlay.qcow2", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("02:00:00:dd:ee:ff", xml)
        self.assertIn("test-infra-net-a1b2c3d4", xml)
        self.assertIn("test-infra-secondary-network-a1b2c3d4", xml)
        self.assertIn(str(ct.VM_MEMORY_KIB), xml)

    def test_haproxy_additions(self):
        use_backends, backends = ct.gen_haproxy_additions("a1b2c3d4", 160)
        self.assertIn("api-a1b2c3d4", use_backends["api"])
        self.assertIn("req_ssl_sni", use_backends["api"])
        self.assertIn("hdr_end(host)", use_backends["ingress-http"])
        self.assertIn("192.168.160.10:6443", backends)
        self.assertIn("192.168.160.10:443", backends)
        self.assertIn("192.168.160.10:80", backends)

    def test_hosts_block(self):
        block = ct.gen_hosts_block("a1b2c3d4")
        self.assertIn("cluster-tool:begin:a1b2c3d4", block)
        self.assertIn("cluster-tool:end:a1b2c3d4", block)
        self.assertIn("10.1.155.16 api.test-infra-cluster-a1b2c3d4.redhat.com", block)
        self.assertIn("console-openshift-console.apps.test-infra-cluster-a1b2c3d4.redhat.com", block)
        self.assertEqual(block.count("10.1.155.16"), len(ct.HOSTS_ENTRIES))


import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestTransactionalBoot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ct.LOCAL_STATE_FILE = Path(os.path.join(self.tmpdir, "state.json"))
        ct.LOCAL_STATE_DIR = Path(self.tmpdir)
        ct.save_state({
            "snapshot": {
                "source_cluster": "6ef80144",
                "source_disk": "/fake/disk",
                "golden_snapshot": "/root/.cluster-tool/golden-snapshot.qcow2",
                "created_at": "2026-01-01T00:00:00Z",
            },
            "clones": {},
            "next_subnet": 160,
        })
        self.calls = []

    def _boot_args(self):
        return argparse.Namespace(name="aabbccdd")

    def _make_ssh_mock(self, fail_on):
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            if fail_on in cmd and check:
                raise SystemExit(f"Simulated: {fail_on}")
            r = MagicMock()
            r.returncode = 0
            r.stdout = "fake-ingress-cn" if "ingress-cn" in cmd else ""
            r.stderr = ""
            return r
        return ssh

    def _cleanup_cmds(self):
        return [cmd for cmd, chk in self.calls if not chk and any(
            k in cmd for k in ["rm -f /data/cluster-tool/overlays",
                                "virsh net-destroy", "virsh net-undefine",
                                "virsh destroy ", "virsh undefine "]
        )]

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_network_cleans_overlay(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("virsh net-define /tmp/net-")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_vm_cleans_overlay_and_networks(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("virsh define /tmp/vm-")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_recert_cleans_everything(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("podman run --rm --name recert")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    @patch.object(ct, "scp_from_baremetal")
    def test_cleanup_runs_in_reverse_order(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("podman run --rm --name recert")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        vm_destroy_idx = next((i for i, c in enumerate(cleanup) if "virsh destroy " in c), -1)
        vm_undefine_idx = next((i for i, c in enumerate(cleanup) if "virsh undefine " in c), -1)
        sec_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-secondary" in c), -1)
        pri_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-net-" in c), -1)
        overlay_idx = next((i for i, c in enumerate(cleanup) if "rm -f" in c), -1)
        self.assertLess(vm_destroy_idx, vm_undefine_idx)
        self.assertLess(vm_undefine_idx, sec_net_destroy_idx)
        self.assertLess(sec_net_destroy_idx, pri_net_destroy_idx)
        self.assertLess(pri_net_destroy_idx, overlay_idx)


    def _make_all_succeed_ssh(self):
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443 test-infra-cluster-aabbccdd-xxxxx"
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r
        return ssh

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_hosts_entries")
    def test_failure_at_hosts_rolls_back_haproxy(self, *_):
        haproxy_removed = []

        def track_remove_haproxy(clone_id):
            haproxy_removed.append(clone_id)

        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()), \
             patch.object(ct, "add_hosts_entries", side_effect=subprocess.CalledProcessError(1, "sudo")), \
             patch.object(ct, "remove_haproxy_clone", side_effect=track_remove_haproxy):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        self.assertEqual(haproxy_removed, ["aabbccdd"])
        self.assertEqual(ct.load_state()["clones"], {})
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertFalse(kubeconfig.exists())

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    def test_called_process_error_triggers_rollback(self, *_):
        def ssh_fail_at_haproxy(cmd, check=True):
            self.calls.append((cmd, check))
            if "cat /etc/haproxy" in cmd and check:
                raise subprocess.CalledProcessError(1, cmd, stderr="connection refused")
            r = MagicMock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_fail_at_haproxy):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("virsh destroy " in c for c in cleanup))
        self.assertTrue(any("net-destroy" in c for c in cleanup))
        self.assertTrue(any("rm -f" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_hosts_entries")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_hosts_entries")
    def test_success_saves_state(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()):
            ct.cmd_boot(self._boot_args())

        state = ct.load_state()
        self.assertIn("aabbccdd", state["clones"])
        self.assertEqual(state["clones"]["aabbccdd"]["subnet_primary"], 160)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertTrue(kubeconfig.exists())
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_hosts_entries")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_hosts_entries")
    def test_recert_uses_key_preservation(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()):
            ct.cmd_boot(self._boot_args())

        recert_cmd = next(cmd for cmd, _ in self.calls if "--use-key" in cmd)
        self.assertIn("--use-key kube-apiserver-lb-signer:/tmp/crypto/lb-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-localhost-signer:/tmp/crypto/localhost-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-service-network-signer:/tmp/crypto/service-network-signer.key", recert_cmd)
        self.assertIn("--use-key fake-ingress-cn:/tmp/crypto/ingress.key", recert_cmd)
        self.assertIn("--cn-san-replace api.test-infra-cluster-6ef80144.redhat.com:api.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace api-int.test-infra-cluster-6ef80144.redhat.com:api-int.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace *.apps.test-infra-cluster-6ef80144.redhat.com:*.apps.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace test-infra-cluster-6ef80144-master-0:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        self.assertIn("--cn-san-replace system:node:test-infra-cluster-6ef80144-master-0,system:node:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)


    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_hosts_entries")
    def test_identity_mismatch_aborts_boot(self, *_):
        def ssh_wrong_identity(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-6ef80144.redhat.com:6443 test-infra-cluster-6e-g677f"
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_wrong_identity):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())

        self.assertIn("IDENTITY MISMATCH", str(ctx.exception))
        self.assertEqual(ct.load_state()["clones"], {})


if __name__ == "__main__":
    unittest.main()
