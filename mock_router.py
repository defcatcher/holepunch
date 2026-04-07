import socket
import threading

def forward_stream(src_sock, dst_sock, client_id):
    try:
        while True:
            chunk = src_sock.recv(65536)
            if not chunk:
                print(f"[-] Client {client_id} disconnected.")
                break
            dst_sock.sendall(chunk)
    except ConnectionResetError:
        print(f"[-] Connection reset by Client {client_id}.")
    except Exception as e:
        print(f"[-] Error on Client {client_id}: {str(e)}")
    finally:
        src_sock.close()
        dst_sock.close()

def main():
    host = '127.0.0.1'
    port = 1488
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(2)
    
    print(f"[+] Mock Router listening on {host}:{port} (Waiting for 2 clients)")
    
    try:
        while True:
            conn1, addr1 = server.accept()
            print(f"[+] Client 1 (Sender) connected from {addr1}")
            
            conn2, addr2 = server.accept()
            print(f"[+] Client 2 (Receiver) connected from {addr2}")
            
            print("[+] Establishing full-duplex pipe between clients...")
            
            t1 = threading.Thread(target=forward_stream, args=(conn1, conn2, 1), daemon=True)
            t2 = threading.Thread(target=forward_stream, args=(conn2, conn1, 2), daemon=True)
            
            t1.start()
            t2.start()
            
    except KeyboardInterrupt:
        print("\n[!] Shutting down router.")
    finally:
        server.close()

if __name__ == "__main__":
    main()