import { useMutation } from "@tanstack/react-query";
import { loginCall, LoginRequest } from "@/components/networking";
import { useAuth } from "@/contexts/AuthContext";
import { getCookie } from "@/utils/cookieUtils";

export const useLogin = () => {
  const { setToken } = useAuth();
  return useMutation({
    onSuccess: (result) => setToken(result.token ?? getCookie("token")),
    mutationFn: async ({ username, password, useV3 }: LoginRequest) => {
      const result = await loginCall(username, password, useV3);
      return result;
    },
  });
};
